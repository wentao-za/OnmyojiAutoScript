# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
"""超鬼王战斗域：召唤 → 合成 → 换魂 → 强力 → 打 → 回鬼王页。

含好友副本（消费他人分享的鬼王）。**不含**门票获取（ticket_farming）
与主动分享（assistance），两者在别的模块。
"""
import random
import re
import time
from datetime import datetime, timedelta

from module.atom.image import RuleImage
from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.QuickLoadout.config import (
    NamedQuickLoadoutConfig,
    QuickLoadoutConfig,
    QuickLoadoutMode,
)
from tasks.Component.QuickLoadout.quick_loadout import QuickLoadout
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.game_ui import GameUi
from tasks.GlobalGame.assets import GlobalGameAssets
import tasks.GameUi.task_pages as ipages

from tasks.MetaDemon.assets import MetaDemonAssets
from tasks.MetaDemon.config import BossType, MetaDemon, boss_cn, loadout_group
from tasks.MetaDemon.synthesis import parse_synthesis_material_level
from tasks.MetaDemon.ticket_farming import TicketFarmingMixin, TicketStatus


class BossBattle(TicketFarmingMixin, GeneralBattle, SwitchSoul, QuickLoadout, GameUi, MetaDemonAssets):
    """超鬼王战斗域。"""

    class State:
        done: bool = False
        synthesis_done: bool = False

    conf: MetaDemon = None
    cur_boss_type: BossType | None = None
    total_count: int = 0
    # 好友副本状态：_friend_mode 表示本场是否打好友的鬼王；
    # _friend_block_until 是"好友副本打不动"后的静默期（时间戳），期间先走自己的流程
    _friend_mode: bool = False
    _friend_block_until: float = 0.0
    # 召唤代次：每次确认召唤出新鬼王 +1。assistance 靠它判断"这是不是新的一只"
    _boss_generation: int = 0
    # 当前已应用的御魂组别（含自定义预设时的代次），用于避免重复进式神录
    _active_loadout_group = None
    # summon_and_fire 的失败原因：True 表示「弹窗里一张可点的票都没有」，
    # 只有这种失败才允许触发补票；召唤超时不算（票可能还在，只是没点出去）。
    _summon_empty: bool = False
    # 驻留等待：无法继续战斗时停留在鬼王界面，直到该时刻收尾（None=非驻留态）
    _idle_deadline: datetime | None = None
    # 驻留触发源，决定复核策略：'tickets'（票尽，无恢复来源）/ 'sequence'（有票但
    # 星级不匹配，限频重试）/ 'fatigue'（疲劳满且开自动喝茶，每拍查疲劳）。
    # 首次进入即定死，后续叠加的触发不改写它，否则票尽与疲劳会互相覆盖、来回抖动
    _idle_reason: str | None = None
    # 驻留态下一次「主动复核」时刻：sequence 触发时用它限频重走正常召唤流程
    _idle_next_resume: float = 0.0
    # 驻留态下一次收紧截止时刻的时刻：驻留期间新到期的任务要能立刻让位
    _idle_next_deadline_check: float = 0.0
    # 今日协助次数已满只警告一次，防止驻留轮询每拍刷屏
    _assist_full_reported: bool = False

    # ------------------------------------------------------------- 生命周期

    def before_run(self):
        # 导航阶段的未知页兜底（goto_page 内部时任务层 run() 的 case _ 轮不到）：
        # 1) 昨日狩猎奖励面板——没有红 X，唯一出口是「领取」，必须先于红 X 判断
        # 2) 礼包/公告类全屏弹窗——点右上角红 X
        self.navigator.add_unknown_closer(self.claim_yesterday_reward, self.close_unexpected_popup)
        # 每轮新建 State：它是类属性，不重建会带着上一轮的 done / synthesis_done 进来
        self.State = BossBattle.State()
        self.conf = self.config.meta_demon
        self.limit_time = self.conf.meta_demon_config.limit_time_v
        self.limit_count = self.conf.meta_demon_config.limit_count
        self.cur_boss_type = None
        self.total_count = 0
        self._friend_mode = False
        self._friend_block_until = 0.0
        self._boss_generation = 0
        self._active_loadout_group = None
        self._summon_empty = False
        self._idle_deadline = None
        self._idle_reason = None
        self._idle_next_resume = 0.0
        self._idle_next_deadline_check = 0.0
        self._assist_full_reported = False
        self.reset_popup_state()

        page_shikigami_records = self.navigator.resolve_page(ipages.page_shikigami_records)
        page_meta_demon_boss = self.navigator.resolve_page(ipages.page_meta_demon_boss)
        page_main = self.navigator.resolve_page(ipages.page_main)
        if page_shikigami_records is None or page_meta_demon_boss is None or page_main is None:
            raise RuntimeError("MetaDemon 页面 session 初始化失败")

        # 仅修改当前任务 session 内的跳转图，不污染全局页面定义。
        # 只补「鬼王页 -> 式神录」这条真实边（点 I_MD_SHIKIGAMI 进入）。
        # 不再伪造「式神录 -> 鬼王页」：式神录的返回按钮回到进入前的来源页，
        # 伪造该边会让导航器把式神录当成通往鬼王页的中转站，规划出
        # 庭院 -> 探索 -> 个人结界突破 -> 式神录 -> 鬼王页 的脏路径。
        # 同时保留式神录原有的「-> 庭院」返回边与庭院直达式神录的直连。
        page_meta_demon_boss.connect(
            page_shikigami_records,
            MetaDemonAssets.I_MD_SHIKIGAMI,
            key="page_meta_demon_boss->page_shikigami_records",
        )

    def _meta_demon_battle_key(self) -> str:
        """返回当前鬼王类型对应的通用战斗分组键。

        三段式下按御魂组别区分（2 个 key），不再按星级（6 个）。
        """
        if self.cur_boss_type is None:
            return "meta_demon_default"
        return f"meta_demon_{loadout_group(self.cur_boss_type)}"

    @property
    def enable_powerful_fire(self):
        powerful_list = self.conf.meta_demon_config.powerful_list_v
        return self.cur_boss_type is not None and self.cur_boss_type in powerful_list

    def update_global_state(self):
        if datetime.now() - self.start_time >= self.limit_time or \
                self.total_count >= self.limit_count:
            self.State.done = True

    def finish_task(self):
        self.finish_task_navigation()
        raise TaskEnd

    # ------------------------------------------------------------ 驻留等待

    def _next_task_start(self) -> datetime | None:
        """估算「下一任务最早开始时刻」，驻留应在该时刻前收尾。

        三分支（module/config/config.py update_scheduler 的实测语义）：
        - 已有其他任务到期（pending_task）→ now，没有等待余地，立刻收；
        - 有 waiting 任务 → 其 next_run − idle_wait_buffer_min（防风控看守器
          与攒批只会推迟任务、不会提前，提前收尾安全）；
        - 没有其他启用任务 → start_time + limit_time，驻留到本任务限时为止。
        必须用 isinstance 过滤：update_scheduler 会把 next_run 非 datetime 的
        error 任务前置塞进 pending_task，不过滤则驻留永不触发；也必须排除
        MetaDemon 自己（此刻它的 next_run 大概率已到期）。
        """
        self.config.update_scheduler()
        now = datetime.now()
        for func in self.config.pending_task:
            if func.command != 'MetaDemon' and isinstance(func.next_run, datetime):
                return now
        for func in self.config.waiting_task:
            if func.command != 'MetaDemon' and isinstance(func.next_run, datetime):
                buffer = timedelta(minutes=self.conf.meta_demon_config.idle_wait_buffer_min)
                return max(now, func.next_run - buffer)
        return self.start_time + self.limit_time

    def _assist_count_full(self) -> bool | None:
        """今日协助次数是否已满；OCR 读不到返回 None（fail-open 放行）。

        协助计数是优化不是闸门：误拦会放弃真实战斗，漏拦的后果本就有
        start_battle 的 20 秒超时 + 2 分钟好友静默兜底。
        """
        raw = str(self.O_MD_ASSIST_COUNT.ocr(self.device.image) or '')
        m = re.search(r'(\d+)\s*/\s*(\d+)', raw)
        if not m:
            return None
        return int(m.group(1)) >= int(m.group(2))

    def _idle_refresh_deadline(self) -> None:
        """驻留期间周期性收紧截止时刻。

        进入驻留时算出的 deadline 是快照；若这期间有新任务到期（用户临时改配置、
        手动补任务），`_next_task_start()` 会返回 now，必须立刻让位。
        只允许提前、不允许推后，避免驻留被反复延长成「永不结束」。
        """
        now_ts = time.time()
        if now_ts < self._idle_next_deadline_check:
            return
        self._idle_next_deadline_check = now_ts + 30
        if self._idle_deadline is None:
            return
        deadline = self._next_task_start()
        if deadline is None or deadline >= self._idle_deadline:
            return
        logger.info(f'驻留期间检测到更早的任务起点，驻留截止提前至 {deadline:%H:%M:%S}')
        self._idle_deadline = deadline

    def _idle_try_resume(self) -> bool:
        """驻留复核：条件恢复就退出驻留、回正常战斗流程（用户 2026-11-06 拍板「可恢复」）。

        - fatigue：每拍读疲劳度，不再是满就退出 —— 喝茶或自然恢复后立刻复打；
        - sequence：限频放行一次正常流程（合成可能合出序列内的票）；若仍不匹配，
          `summon_and_fire` 会重新进驻留并把复核时刻推后，形成低频重试；
        - tickets：补票已用尽、没有恢复来源，不复核，驻留到截止时刻。
        """
        if self._idle_reason == 'fatigue' and not self._fatigue_full():
            logger.info('疲劳已恢复，退出驻留回到正常战斗流程')
            self._exit_idle_wait()
            return True
        if self._idle_reason == 'sequence' and time.time() >= self._idle_next_resume:
            logger.info('驻留复核：尝试回到正常召唤流程（门票星级可能已变化）')
            self._exit_idle_wait()
            return True
        return False

    def _idle_poll_friend_boss(self) -> None:
        """驻留期的一拍：清卡死记录 -> 复核是否恢复 -> 轮询好友分享的鬼王 -> 接战。

        - 每拍 `device.stuck_record_clear()`：上供模式同款先例，刻意静止不算卡死；
        - 紧跟 `close_residual_popup()`：漏关的召唤队列弹窗会压住好友分享素材 ROI，
          先关再判；它自带限流，未命中时约 0.2 秒返回；
        - 先做 `_idle_try_resume()`：疲劳恢复 / 门票星级恢复就退出驻留，本拍直接返回，
          下一拍照常走正常战斗流程；
        - `check_fatigue()` 只在命中好友副本、接战之前调用：未开自动喝茶时它内部
          会 set_next_run 并抛 TaskEnd（驻留随之结束）；已开自动喝茶时若本轮刚转
          驻留则返回 False 不接战，其余情况放行 —— 喝茶窗口只在点「挑战」时弹出；
        - 好友副本剩余时间少于 idle_battle_min_remain 分钟不接战，等它过期。
        """
        self.device.stuck_record_clear()
        # 残余弹窗自愈：`try_refill_ticket()` 收尾时 `close_summon_panel()` 可能因
        # `appear` 的 interval 冷却整段跳过（判「暂无裂变之眼」的计时器刚被
        # `judge_drop_progress()` 重置），召唤队列弹窗就会一直压在鬼王页上，
        # 而好友分享素材 ROI 右缘有约 18px 在它下面 —— 不先关掉会整段驻留漏判。
        # 放最前面（早于 `_idle_try_resume`）：本拍若退出驻留回正常流程，界面也已是干净的。
        if self.close_residual_popup():
            return
        self._idle_refresh_deadline()
        if self._idle_try_resume():
            return
        poll = max(2, int(self.conf.meta_demon_config.idle_poll_interval))
        if self.check_and_switch_friend_boss():
            if not self.check_fatigue():
                # 疲劳刚满且开茶：本拍不接战，交给下一拍驻留轮询继续
                return
            remain = self.ocr_seconds(self.O_MD_FRIEND_REMAIN.ocr(self.device.image))
            min_remain = self.conf.meta_demon_config.idle_battle_min_remain * 60
            if remain is not None and remain < min_remain:
                logger.info(f'好友副本剩余 {remain}s，不足单场估算 {min_remain}s，本次不接战')
            else:
                self.start_battle()
                self._friend_mode = False
                return
        time.sleep(poll)

    # ------------------------------------------------------------ 页面主循环

    def run_battle_loop(self) -> bool:
        """鬼王战斗主循环：页面状态机。由 ScriptTask.run() 分派调用。

        只负责跑完整个循环并返回是否正常收尾，`set_next_run` / `finish_task`
        统一交给分派层（D：assistance 与本方法都返回 bool，由 ScriptTask 收尾）。
        """
        self.before_run()
        self.goto_page(ipages.page_meta_demon_boss)
        while True:
            if self._idle_deadline is not None:
                # 驻留态：limit_time 不再约束（驻留本身就是填满任务间隙），只看驻留截止
                if datetime.now() >= self._idle_deadline:
                    logger.info('驻留等待时间到，收尾退出')
                    break
            else:
                self.update_global_state()
            if self.State.done:
                break
            self.screenshot()
            # 领取奖励面板会盖住 page_meta_demon 的检查位，页识别必然失败；
            # 放在判页之前处理，避免被当成未知页交给返回键空转
            if self.claim_yesterday_reward():
                continue
            current_page = self.get_current_page()
            match current_page:
                case ipages.page_meta_demon:
                    self.goto_page(ipages.page_meta_demon_boss)
                case ipages.page_meta_demon_boss:
                    if self._idle_deadline is not None:
                        self._idle_poll_friend_boss()
                    elif self.check_and_prepare_battle():
                        self.start_battle()
                case ipages.page_battle:
                    self.battle_wait(self.conf.battle_config(self._battle_category()).random_click_swipt_enable)
                case ipages.page_reward | ipages.page_battle_result:
                    self.click(ipages.random_click(), interval=0.6)
                case _:
                    # 判页失败大多是意外弹窗（礼包/公告/商城推送）盖住了界面。
                    # 先点右上角红 X 把界面捞回来，否则会一直空转到 device 的
                    # 卡死保护，45 秒后抛 GameStuckError 重启。
                    if not self.close_unexpected_popup():
                        time.sleep(random.uniform(0.7, 1.4))
        return True

    def _battle_category(self) -> str:
        """当前战斗该用哪一组战斗参数。未识别星级按低星处理。"""
        return loadout_group(self.cur_boss_type)

    # ------------------------------------------------------------ 战斗准备

    def check_and_prepare_battle(self) -> bool:
        """检查并做战斗前的准备工作
        :return: True可以战斗, False不能战斗
        """
        logger.hr('战斗准备', 2)
        if not self.check_fatigue():
            return False
        # 好友分享的可战斗副本优先级最高：命中就跳过合成、开自己队列、切御魂，
        # 直接打好友那只（协助次数上限由 start_battle 的超时兜底）。
        # render_wait=1.0：战后回页一瞬间卡片常来不及渲染，等 1 秒再判定
        if self.check_and_switch_friend_boss(render_wait=1.0):
            powerful = '开启' if self.appear(self.I_MD_DISABLE_POWERFUL_FIRE, interval=0.4) else '关闭'
            logger.info(f'好友副本：沿用当前御魂与强力追击设置（强力{powerful}），不切换')
            return True
        self.synthesis_boss_ticket()
        # 门票：前置式已在分派层刷满，这里只判库存/开窗；不足时走反应式兜底补票
        status = self.ensure_ticket()
        if status is TicketStatus.BLOCKED:
            return False
        if status is TicketStatus.SUMMON:
            if not self.summon_and_fire():
                # 弹窗还开着：交给门票域判掉落进度，决定补票还是收尾本轮
                self.exhaust_ticket()
                return False
        return self.check_and_switch_soul() and self.check_and_switch_powerful()

    def _fatigue_full(self) -> bool:
        """疲劳度是否已满。DigitCounter 读出的计数越界（current > total）即视为满。"""
        current, remain, total = self.O_MD_FATIGUE.ocr_digit_counter(self.device.image)
        return current > total or remain < 0

    def check_fatigue(self) -> bool:
        """疲劳度闸门，返回 False 表示本轮不要接战。

        三分支（2026-11-06 用户拍板）：
        - 未满：放行；
        - 满且未开自动喝茶：结束流程（set_next_run + TaskEnd，与 dev 原行为一致）；
        - 满且已开自动喝茶：首次转入驻留并返回 False（本拍不接战）；已在驻留态则
          返回 True —— 喝茶窗口只在点「挑战」时弹出，禁止接战就永远喝不到茶，
          疲劳也永远恢复不了。驻留轮询每拍复查，恢复后自动退出驻留。
        """
        logger.hr('检查疲劳度')
        if not self._fatigue_full():
            return True
        if not self.conf.meta_demon_config.auto_tea:
            logger.info('疲劳已满且未开启自动喝茶，结束本次运行')
            self.set_next_run('MetaDemon', target=datetime.now() + self.conf.scheduler.wait_interval)
            self.finish_task()
        if self._idle_deadline is not None:
            return True
        if self._enter_idle_wait('fatigue'):
            logger.info('疲劳已满且已开启自动喝茶，转入驻留等待（仍接好友副本，接战即可触发喝茶）')
            return False
        logger.warning('疲劳已满、已开启自动喝茶，但驻留开关未启用，维持原行为继续战斗')
        return True

    # ---------------------------------------------------------------- 合成

    def synthesis_boss_ticket(self):
        """合成鬼王"""
        if self.State.synthesis_done:
            return
        logger.hr('合成鬼王门票')
        self.State.synthesis_done = True
        if len(self.conf.meta_demon_config.synthesis_list_v) <= 0:
            return
        type_ticket_dict: dict[BossType, tuple[RuleImage, int]] = {
            BossType.ONE_STAR: (self.I_MD_SYNTHESIS_ONE_STAR, 3),
            BossType.TWO_STARS: (self.I_MD_SYNTHESIS_TWO_STAR, 3),
            BossType.THREE_STARS: (self.I_MD_SYNTHESIS_THREE_STAR, 3),
            BossType.FOUR_STARS: (self.I_MD_SYNTHESIS_FOUR_STAR, 3),
            BossType.FIVE_STARS: (self.I_MD_SYNTHESIS_FIVE_STAR, 2),
        }
        synthesis_list = self.conf.meta_demon_config.synthesis_list_v
        self.ui_click(self.I_MD_SYNTHESIS, self.I_MD_START_SYNTHESIS, interval=0.6)
        for boss_type in synthesis_list:
            # 第三个参数是「材料星级」，用于点卡后反查面板标题是否真的切到了目标星级
            self.do_synthesis(type_ticket_dict[boss_type][0], type_ticket_dict[boss_type][1], boss_type)
        # 关闭合成弹窗退回到鬼王界面。注意礼包/商城类弹窗是半透明的，底部「式神录」
        # 按钮会透出来（22:33 崩溃帧实测 0.947 HIT），所以不能拿它当「已回到鬼王界面」
        # 的判据——否则会带着弹窗继续往下走，紧接着召唤入口 15 秒 0 命中。
        # 改成「点 X 直到 X 消失」，以弹窗自身为判据。
        if self.appear(self.I_MD_CLOSE_POPUP, interval=0.6):
            self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6, timeout=8)

    def do_synthesis(self, target_boss_ticket: RuleImage, need_ticket: int,
                     product_level: BossType | None = None):
        """开始合成鬼王
        :param target_boss_ticket: 目标鬼王级别门票
        :param need_ticket: 合成当前级别鬼王需要的门票数量
        :param product_level: 配置要求的**产物**星级（= synthesis_list 里的值）；None 表示不校验
        """
        if not self.appear(target_boss_ticket):
            logger.info(f'未识别到门票素材 {target_boss_ticket}，跳过该星级合成')
            return
        click_cnt, max_cnt = 0, random.randint(2, 3)
        verify_fail_cnt = 0
        # 护栏：插入"重截图 + OCR 校验"后时序变长，若卡片和开始合成同时点不到，
        # 原实现的 while 会原地空转（click_cnt 只在点成功时累加，空转永远到不了 max_cnt）
        idle_timer = Timer(10).start()
        while True:
            self.screenshot()
            if click_cnt >= max_cnt:  # 兜底,多次点击开始合成都没有出现获得奖励则退出
                break
            if self.appear(self.I_MD_SYNTHESIS_NEED_MONEY, interval=0.8):  # 出现花钱购买合成券
                # 券不足时游戏推的是「宠梦礼包」全屏页，不是小购买弹窗。该页有两个 X 形命中：
                # 页内真 X 在 (1201,116) 得 0.924；通用 I_MD_CLOSE_POPUP 的 roi 里还罩着一个被
                # 半透明层压暗的 X（1111,75），得 0.933 —— 分数只差 0.009 且暗的更高，
                # minMaxLoc 必然取到死点，点它画面零变化，连点 10 次撞 device.py 硬限。
                # 礼包页改用 roi 收窄到 (1150,70,110,100) 的专属 X，死点物理上落在窗外。
                if self.appear(self.I_MD_GIFT_CLOSE, interval=0.3):
                    self.appear_then_click(self.I_MD_GIFT_CLOSE, interval=0.6)
                    time.sleep(0.5)  # 等礼包页淡出，避免下一星级把「未识别到门票素材」误判
                else:
                    self.ui_click(self.I_MD_CLOSE_POPUP, self.I_MD_START_SYNTHESIS, interval=0.6)  # 关闭购买弹窗
                break
            if self.appear(self.I_UI_REWARD, interval=0.8):  # 出现获得奖励说明合成成功
                self.click(ipages.random_click(), interval=0.6)
                click_cnt = 0  # 清空点击标志
                idle_timer.reset()
                continue
            if self.appear(self.I_MD_SYNTHESIS_EMPTY, interval=0.8):  # 有空位
                empty_list = self.I_MD_SYNTHESIS_EMPTY.match_all(self.device.image, frame_id=self.device.image_frame_id)
                if len(empty_list) < need_ticket:  # 空位数量小于需要的门票数量->门票不够则退出
                    logger.info(f'{target_boss_ticket} 空位不足，跳过该星级合成')
                    break
            if self.appear_then_click(target_boss_ticket, interval=2):  # 点击对应级别鬼王门票
                idle_timer.reset()
                # 选中后必须重新截图再验证：同一帧里点卡与读标题是两个时刻，
                # 点之前那一帧的标题还是上一张卡的内容
                time.sleep(0.3)
                self.screenshot()
                if self._verify_synthesis_level(product_level) is False:
                    verify_fail_cnt += 1
                    logger.warning(f'{boss_cn(product_level)}鬼王合成星级校验未通过'
                                   f'（第 {verify_fail_cnt}/3 次），重新选卡')
                    if verify_fail_cnt >= 3:
                        logger.warning(f'{target_boss_ticket} 连续 3 次无法确认目标星级，跳过该星级合成')
                        break
                    continue
                if self.appear_then_click(self.I_MD_START_SYNTHESIS, interval=2.5):  # 点击开始合成
                    click_cnt += 1
                    idle_timer.reset()
                continue
            if self.appear_then_click(self.I_MD_START_SYNTHESIS, interval=1.5):
                # 卡片已被详情卡替换时仍能直接点开始合成（实测 13:05 日志：点卡 147ms 后即点合成）
                click_cnt += 1
                idle_timer.reset()
                continue
            if idle_timer.reached():
                logger.warning(f'{target_boss_ticket} 10 秒内既点不到门票卡也点不到开始合成，退出该星级')
                break
        logger.info(f'{target_boss_ticket} 合成完成')

    def _verify_synthesis_level(self, product_level: BossType | None) -> bool | None:
        """校验合成面板左侧详情卡显示的产物星级是否与配置一致。

        三态返回，**调用方必须区分 None 与 False**：
        - True  标题产物星级 = 配置星级，可以点「开始合成」
        - False 标题产物星级**确认**不等于配置，应重新选卡（累计 3 次即跳过该星级）
        - None  读不到 / 解析不出（roi 偏移、弹窗位移、动画残影、一星无星级标题），
                属于"没有证据"，**一律放行**。把 None 当失败会让 OCR 一失准就
                永久跳过合成，比不校验更糟。

        实测（`md_adb_now.png`，1280x720）：
        - 标题「四星裂变之眼」文字 bbox x=311..455 / y=156..181，roi 取 (305,148,165,40)；
        - 该帧 `I_MD_START_SYNTHESIS` 命中 1.000 @(906,562)，确认同一套 UI；
        - 副标题写着「相同星级可以合成高一级裂变之眼」，所以**标题是产物星级，
          消耗的是低一级材料**，比较时必须 -1。
        """
        if product_level is None:
            return None
        text = self.O_SYNTHESIS_LEVEL.ocr(self.device.image)
        detected_material = parse_synthesis_material_level(text)
        if detected_material is None:
            logger.warning(f'合成星级无法识别（OCR={text!r}），按未验证继续')
            return None
        expected_material = int(product_level) - 1
        if detected_material != expected_material:
            logger.warning(f'合成星级不符：面板标题={text!r}（材料 {detected_material} 星）'
                           f'/ 配置产物 {boss_cn(product_level)}（材料应 {expected_material} 星）')
            return False
        logger.info(f'合成星级校验通过：{detected_material} 星材料 → {detected_material + 1} 星鬼王')
        return True

    # ---------------------------------------------------------------- 召唤

    def summon_and_fire(self, sequence: list[BossType] | None = None) -> bool:
        """在**已打开的**召唤队列弹窗里选卡并点「立即召唤」。

        :param sequence: 限定可召唤的星级序列；None 表示用配置的 fire_sequence_v。
                         assistance 靠它只召唤六星。
        前置契约：调用方（ticket_farming.ensure_ticket）保证弹窗处于打开态。
        失败契约：返回 False 时弹窗**可能仍处于打开态**；只有 `_summon_empty`
        为 True（弹窗里一张可点的票都没有）才把弹窗交给 `exhaust_ticket` 处理，
        召唤超时、配置与库存不匹配两种情况由本方法自己收尾。
        """
        type_ticket_dict: dict[BossType, RuleImage] = {
            BossType.ONE_STAR: self.I_MD_ONE_STAR,
            BossType.TWO_STARS: self.I_MD_TWO_STARS,
            BossType.THREE_STARS: self.I_MD_THREE_STARS,
            BossType.FOUR_STARS: self.I_MD_FOUR_STARS,
            BossType.FIVE_STARS: self.I_MD_FIVE_STARS,
            BossType.SIX_STARS: self.I_MD_SIX_STARS,
        }
        source = list(sequence) if sequence is not None else self.conf.meta_demon_config.fire_sequence_v
        source = [boss_type for boss_type in source if boss_type in type_ticket_dict]
        self._summon_empty = False

        # 获取弹窗里所有存在的鬼王门票（不按 sequence 预过滤：要区分
        # 「一张票都没有」和「有票但不是要打的星级」，前者才允许补票）
        panel_cards = [boss_type for boss_type in type_ticket_dict
                       if self.appear(type_ticket_dict[boss_type])]
        if len(panel_cards) <= 0:
            # 弹窗淡入时可能「立即召唤」已出、门票卡片尚未渲染，此时直接判无票会把还剩下的票丢掉；
            # 先给 3 秒重试窗口，把半渲染帧和真无票区分开
            retry_timer = Timer(3).start()
            while not retry_timer.reached():
                time.sleep(0.4)
                self.screenshot()
                panel_cards = [boss_type for boss_type in type_ticket_dict
                               if self.appear(type_ticket_dict[boss_type])]
                if panel_cards:
                    logger.info(f'重试后识别到可召唤门票：{boss_cn(panel_cards[0])}鬼王')
                    break
        if len(panel_cards) <= 0:
            logger.warning('召唤队列里没有任何可用的鬼王门票')
            self._summon_empty = True
            # 弹窗保持打开：exhaust_ticket 要在这个弹窗里读掉落进度 (x/50)
            return False

        # 根据存在的鬼王门票和配置的鬼王序列过滤出需要召唤的鬼王门票
        need_fire_list = [boss_type for boss_type in source if boss_type in panel_cards]
        if len(need_fire_list) <= 0:
            # 有票但没有一个是配置要打的星级：属于「配置与库存不匹配」，不是「没票」，
            # 不能触发补票（会白刷一趟）。主战斗路径转入驻留 —— 合成或票池变化后可能
            # 恢复，驻留期间还能持续轮询好友副本；assistance 是显式传 sequence
            # （只召唤六星），必须保持「结束本轮」语义，不能改写它的收尾。
            logger.info('现有门票与配置的攻击顺序无交集，没有需要攻击的鬼王')
            self.close_summon_panel()
            if sequence is None and self._enter_idle_wait('sequence'):
                logger.info('转入驻留等待：继续轮询好友分享副本，并定期复核门票星级')
                return False
            self.State.done = True
            return False
        target_type = need_fire_list[0]
        target_ticket_rule_image = type_ticket_dict[target_type]
        selected = False
        logger.info(f'召唤{boss_cn(target_type)}鬼王')
        # 有界退出：门票图标失效或召唤无响应时不能无限空转（会绕过 run() 的限时检查）
        timer = Timer(30).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_MD_FIRE):  # 可以挑战鬼王则退出
                if self.cur_boss_type is None or \
                        loadout_group(self.cur_boss_type) != loadout_group(target_type):
                    # 组别变化才清零轮次；同组共用同一套御魂与阵容
                    self.current_count = 0
                self.cur_boss_type = target_type
                self._boss_generation += 1
                logger.info(f'{boss_cn(target_type)}鬼王已就位')
                return True
            if selected and self.appear(self.I_MD_CHECK_SELECTED, interval=0.6):
                selected_x, _ = self.I_MD_CHECK_SELECTED.coord()
                x, _, w, _ = target_ticket_rule_image.roi_front
                if x <= selected_x <= x + w:  # 确定选中目标鬼王门票, 召唤鬼王
                    self.ui_click_until_disappear(self.I_MD_GET_BOSS, interval=0.6, timeout=15)
                    continue
            if self.appear_then_click(target_ticket_rule_image, interval=0.6):  # 选择对应鬼王门票
                selected = True
                continue
        logger.warning('30 秒内未能召唤出鬼王，跳过本轮')
        return False

    # ---------------------------------------------------------------- 换魂

    def check_and_switch_soul(self) -> bool:
        """按鬼王组别应用御魂；高星开启快速上阵时通过 frame 装魂并上阵。"""
        ss = self.conf.switch_soul_config
        if self.cur_boss_type is None:
            # 星级未知：只要任一段配置了切换 / 预设 / 快速上阵，就不该盲目开打
            enabled = any(
                ss.switch_for(group).enable or ss.switch_for(group).enable_switch_by_name
                or self.conf.battle_config(group).preset_enable
                or self.conf.general_config.high_star_quick_loadout_enable
                for group in ('low_star', 'high_star')
            )
            if enabled:
                logger.error('当前鬼王类型未知，但配置了御魂切换，跳过本轮')
                return False
            return True

        category = loadout_group(self.cur_boss_type)
        soul = ss.switch_for(category)
        custom = self.conf.quick_preset_config
        quick_enable = bool(category == 'high_star' and self.conf.general_config.high_star_quick_loadout_enable)
        if quick_enable and not (soul.group_name and soul.team_name):
            # 快速上阵只认「分组名,预设名」；配成编号时无从识别，回退普通切换
            logger.error('高星快速上阵需要把高星御魂配置写成「分组名,预设名」，当前为空，回退为普通切换')
            quick_enable = False
        custom_enabled = bool(quick_enable and custom.high_star_custom_preset_enable)
        # 自定义预设每来一只新鬼王都要重新上阵，所以缓存键带上代次
        loadout_key = (category, self._boss_generation) if custom_enabled else category
        if self._active_loadout_group == loadout_key:
            return True

        logger.hr('切换御魂')
        if quick_enable:
            if custom_enabled:
                quick = NamedQuickLoadoutConfig(
                    enable=True, mode=QuickLoadoutMode.OCR,
                    group_name=soul.group_name, preset_name=soul.team_name,
                    custom_preset_enable=True, custom_preset=custom.high_star_custom_preset,
                )
            else:
                # mode 必须显式给 OCR：默认 NUMBER 会走 group_number/preset_number，
                # 静默用成 (1,1) 预设，不报任何错
                quick = QuickLoadoutConfig(
                    enable=True, mode=QuickLoadoutMode.OCR,
                    group_name=soul.group_name, preset_name=soul.team_name,
                )
            if not self.run_quick_loadout(
                    quick,
                    frame=self.F_QUICK_LOADOUT_FRAME,
                    name_ocr=self.O_BOSS_NAME if custom_enabled else None,
            ):
                return False
        elif soul.enable or soul.enable_switch_by_name:
            if not self._enter_shikigami_records():
                # 点不到「式神录」时退回导航器；此时图上只剩真实边 鬼王页 -> 式神录
                self.goto_page(ipages.page_shikigami_records)
            if soul.enable_switch_by_name:
                self.run_switch_soul_by_name(soul.group_name, soul.team_name)
            elif soul.enable:
                switch_type, group_team = _split_group_team(soul.switch_group_team)
                if switch_type is None:
                    logger.error(f'{category} 御魂分组格式无效：{soul.switch_group_team!r}')
                    self._leave_shikigami_records()
                    return False
                self.run_switch_soul(group_team)
            self._leave_shikigami_records()

        # 其他组别可能已改变阵容，切回时必须重新应用该组别的战斗预设
        shared = getattr(self, '_battle_shared_state', None)
        if shared is not None:
            shared.pop(f'meta_demon_{category}', None)
        self._active_loadout_group = loadout_key
        return True

    def _enter_shikigami_records(self) -> bool:
        """从鬼王页点「式神录」进入，最多等 12 秒。

        不交给导航器：导航器的到页判定只等 4 秒，而模拟器转场常要 8-10 秒，
        会被判失败并给这条边累加 penalty。
        """
        timer = Timer(12).start()
        last_click, clicks = 0.0, 0
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_CHECK_RECORDS):
                return True
            now = time.time()
            if clicks < 3 and now - last_click >= 3.0:
                if self.appear_then_click(self.I_MD_SHIKIGAMI, interval=0.6):
                    last_click, clicks = now, clicks + 1
                    continue
            time.sleep(0.3)
        logger.warning('12 秒内未进入式神录')
        return False

    def _leave_shikigami_records(self) -> None:
        """换魂结束按返回回到鬼王页（返回回到进入前的来源页，即鬼王页）。"""
        timer = Timer(10).start()
        last_click, clicks = 0.0, 0
        while not timer.reached():
            self.screenshot()
            if self.is_meta_demon_boss_page():
                logger.info('已返回鬼王页面')
                return
            now = time.time()
            if clicks < 3 and now - last_click >= 3.0:
                if self.appear_then_click(GlobalGameAssets.I_UI_BACK_YELLOW, interval=0.8):
                    last_click, clicks = now, clicks + 1
                    continue
            time.sleep(0.3)
        logger.warning('点击返回未能回到鬼王页，改用导航器')
        self.goto_page(ipages.page_meta_demon_boss)

    # ---------------------------------------------------------------- 强力

    def check_and_switch_powerful(self) -> bool:
        """检查并切换是否开启强力"""
        logger.hr('设置强力追击')
        if not self.appear(self.I_MD_FIRE):
            self.goto_page(ipages.page_meta_demon_boss)
            self.screenshot()
            if not self.appear(self.I_MD_FIRE):  # 处于boss界面但是没有挑战按钮
                return False
        powerful_list = self.conf.meta_demon_config.powerful_list_v
        # 当前boss类型未知或不在强力列表中则使用普通追击
        if self.cur_boss_type is None or self.cur_boss_type not in powerful_list:
            self.ui_click(self.I_MD_ENABLE_POWERFUL_FIRE, self.I_MD_DISABLE_POWERFUL_FIRE, interval=0.6)
            return True
        # 已知类型且在强力列表中则开启强力追击
        self.ui_click(self.I_MD_DISABLE_POWERFUL_FIRE, self.I_MD_ENABLE_POWERFUL_FIRE, interval=0.6)
        return True

    # ------------------------------------------------------------ 好友副本

    def _is_viewing_friend_boss(self) -> bool | None:
        """当前界面显示的鬼王是不是好友分享的那一只。

        判据：中央「XX:XX:XX 后逃跑」与左侧好友卡片「剩余时间」是同一个计时器，
        两者相等（±2 秒，两次 OCR 之间有耗时）就说明中央显示的是好友的鬼王。
        实测（2026-11-04）：已切好友 01:28:00 / 01:28:00 相等；
        自家的 01:28:05 / 00:58:48 相差 29 分钟，不会误判。
        任一 OCR 失败返回 None，由调用方降级处理。
        """
        friend = self.ocr_seconds(self.O_MD_FRIEND_REMAIN.ocr(self.device.image))
        escape = self.ocr_seconds(self.O_MD_BOSS_ESCAPE.ocr(self.device.image))
        if friend is None or escape is None:
            logger.warning(f'好友副本倒计时识别失败（剩余时间={friend}，逃跑倒计时={escape}）')
            return None
        logger.info(f'倒计时比对：好友剩余 {friend}s / 中央逃跑 {escape}s')
        return abs(friend - escape) <= 2

    def check_and_switch_friend_boss(self, render_wait: float = 0.0) -> bool:
        """好友分享的可战斗副本优先：发现即切过去直接打。

        :param render_wait: 截图判定前的等待秒数。战后回页的一瞬间分享卡片
            可能尚未渲染完成，调用方传 1.0 给足余量；挑战前补判已自带等待，
            驻留轮询节奏慢，默认 0 即可。

        实测（2026-11-04，1280x720 四张帧）：
        - I_MD_FRIEND_SHARED：有分享 0.991~0.993，无分享「暂无好友发现鬼王」0.154；
        - 自家鬼王召唤后与好友副本**都显示「挑战」**（I_MD_FIRE 同坐标 0.998），
          所以 I_MD_FIRE 不能当「切换成功」的判据，只能靠倒计时比对；
        - 好友鬼王星级在界面上不可见，因此不切御魂；也不能按 powerful_list 决定
          强力追击 —— check_and_switch_powerful 里的 ui_click 是无界循环，
          素材不匹配时会死循环，好友模式一律沿用当前设置。
        """
        if render_wait:
            time.sleep(render_wait)
        self.screenshot()
        if time.time() < self._friend_block_until:
            return False
        if not self.appear(self.I_MD_FRIEND_SHARED, interval=0.6):
            # 冒烟信号：分享图没匹配上，但好友卡倒计时 OCR 却读到了有效时间
            # —— 大概率卡片移位（如多好友同时分享）导致搜索区没罩住，
            # 留一条 warning，下次结合截图即可定位（而不是静默放过）
            if self.ocr_seconds(self.O_MD_FRIEND_REMAIN.ocr(self.device.image)) is not None:
                logger.warning('好友分享图未命中，但倒计时已读到内容：疑似卡片移位，请截图反馈')
            return False
        if self._is_viewing_friend_boss():
            logger.info('当前已在好友分享的副本内，直接挑战')
            self._friend_mode = True
            return True
        # 今日协助次数闸门：打好友分享的副本消耗「今日协助」次数，
        # 打满后不切换（读不到计数时 fail-open 放行，见 _assist_count_full）
        if self._assist_count_full():
            if not self._assist_full_reported:
                self._assist_full_reported = True
                logger.warning('今日协助次数已满，本场及之后不再切换好友分享的副本')
            return False
        logger.info('发现好友分享的可战斗副本，切换到该副本')
        self.click(self.C_MD_FRIEND_BOSS, interval=0.8)
        time.sleep(1.2)  # 等中央鬼王和按钮刷新
        self.screenshot()
        if self._is_viewing_friend_boss():
            logger.info('已切换到好友分享的副本')
            self._friend_mode = True
            return True
        # 兜底：切不过去但眼前已有挑战按钮，就打这一只，避免空转
        if self.appear(self.I_MD_FIRE, interval=0.6):
            logger.warning('未能确认已切到好友副本，但当前已有挑战按钮，直接战斗')
            self._friend_mode = True
            return True
        logger.warning('好友副本切换失败且没有挑战按钮，回退自己的流程')
        return False

    # ---------------------------------------------------------------- 战斗

    def start_battle(self):
        logger.hr('点击挑战，开始战斗')
        # 点「挑战」前等 2 秒补判一次好友分享：战斗准备里的好友判定只在战后
        # 回页的一瞬执行，自家鬼王刚召唤出来、分享卡片此刻才渲染完成的情形
        # 常被错过（2026-11-06 oas1：挂着分享却打自己的）。此刻正是分享
        # 可见的高发时刻，补判一次就能接住；切换失败则照常挑战自己的鬼王
        if not self._friend_mode and time.time() >= self._friend_block_until:
            time.sleep(2)
            if self.check_and_switch_friend_boss():
                logger.info('挑战前补判命中好友分享，改打好友副本')
        # 有界：好友副本被打死、或协助次数到顶时挑战按钮会消失，
        # 无界 while 会一路空转到 device 层 45 秒卡死保护而重启
        timer = Timer(20).start()
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                self.total_count += 1
                self.run_general_battle(
                    self.conf.battle_config(self._battle_category()),
                    battle_key=self._meta_demon_battle_key(),
                )
                break
            if self.appear(self.I_MD_DRINK_TEA, interval=1.2):  # 出现喝茶弹窗
                if self.conf.meta_demon_config.auto_tea:
                    self.click(self.I_MD_DRINK_TEA)  # 喝茶
                    continue
                # 不喝茶则结束任务安排下一次运行
                self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6)
                self.set_next_run('MetaDemon', target=datetime.now() + self.conf.scheduler.wait_interval)
                self.finish_task()
            if self.appear_then_click(self.I_MD_FIRE, interval=0.8):
                timer.reset()
                continue
            if timer.reached():
                logger.warning('20 秒内未能进入战斗（挑战按钮消失或副本已失效），跳过本轮')
                if self._friend_mode:
                    # 好友副本可能已被打死但卡片还没刷掉：静默 2 分钟，先走自己的流程
                    self._friend_block_until = time.time() + 120
                    self._friend_mode = False
                    logger.warning('好友副本 2 分钟内不再尝试，先走自己的鬼王流程')
                return

    def battle_wait(self, random_click_swipt_enable: bool) -> bool:
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info(f'开始战斗流程，鬼王类型：{boss_cn(self.cur_boss_type)}')
        win = False
        while True:
            self.screenshot()
            if self.appear(self.I_MD_SUMMON_BOSS, interval=0.8) or \
                    self.appear(self.I_MD_SWITCH_TICKET, interval=0.8) or \
                    self.appear(self.I_MD_FIRE, interval=0.8):
                break
            if self.appear(self.I_WIN, interval=0.8):
                win = True
                self.click(ipages.random_click())
                continue
            if self.appear(self.I_FALSE, interval=0.8):
                win = False
                self.click(ipages.random_click())
                continue
            if self.appear(self.I_MD_SETTLING, interval=3.5):
                logger.info('等待战斗结算')
                time.sleep(random.uniform(3, 5))
                continue
        total_run_time = datetime.now() - self.start_time
        logger.info(f'战斗结果：{"胜利" if win else "失败"}')
        logger.info(f'战斗次数：{self.total_count}/{self.limit_count}')
        logger.info(f'已耗时：{total_run_time.total_seconds():.1f}s/{self.limit_time.total_seconds()}s')
        return win


def _split_group_team(text: str) -> tuple[str | None, tuple[int | str, int | str]]:
    """把「1,2」或「组名,预设名」解析成 (类型, (group, team))；无效返回 (None, ...)。

    三段式配置里 group_team 只存数字编号，这里仍做一次校验，
    避免用户把名称填进了编号栏导致 run_switch_soul 拿到字符串。
    """
    parts = [part.strip() for part in str(text).split(',')]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None, (None, None)
    return 'int', (int(parts[0]), int(parts[1]))
