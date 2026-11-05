# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

import random
from datetime import datetime
from module.atom.image import RuleImage
from module.base.timer import Timer
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.GameUi.assets import GameUiAssets

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.MetaDemon.config import MetaDemon, BossType
from tasks.MetaDemon.assets import MetaDemonAssets
import tasks.GameUi.task_pages as ipages

from module.logger import logger
from module.exception import TaskEnd
from module.config.utils import convert_to_underscore
from module.ocr.sub_ocr import Duration

# 日志用中文星级名，避免输出 one_star 这类枚举名
BOSS_TYPE_CN = {
    BossType.ONE_STAR: '一星',
    BossType.TWO_STARS: '二星',
    BossType.THREE_STARS: '三星',
    BossType.FOUR_STARS: '四星',
    BossType.FIVE_STARS: '五星',
    BossType.SIX_STARS: '六星',
    BossType.MONEY: '氪金',
}


def boss_cn(boss_type: BossType | None) -> str:
    """鬼王类型的中文名，未知返回「未知」。"""
    if boss_type is None:
        return '未知'
    return BOSS_TYPE_CN.get(boss_type, boss_type.name)


class ScriptTask(GeneralBattle, SwitchSoul, GameUi, MetaDemonAssets):
    """超鬼王"""

    class State:
        done: bool = False
        switch_soul_done: bool = False
        synthesis_done: bool = False

    conf: MetaDemon = None
    cur_boss_type: BossType | None = None
    total_count: int = 0
    # 好友副本状态：_friend_mode 表示本场是否打好友的鬼王；
    # _friend_block_until 是"好友副本打不动"后的静默期（时间戳），期间先走自己的流程
    _friend_mode: bool = False
    _friend_block_until: float = 0.0
    # 昨日狩猎奖励面板状态：最后一次领取时间 / 冷却截止 / 奖励页点空白次数
    _reward_claim_ts: float = 0.0
    _reward_fail_until: float = 0.0
    _reward_blank_cnt: int = 0

    def _meta_demon_battle_key(self) -> str:
        """返回当前鬼王类型对应的通用战斗分组键。"""

        if self.cur_boss_type is None:
            return "meta_demon_default"
        return f"meta_demon_{self.cur_boss_type.name.lower()}"

    @property
    def enable_powerful_fire(self):
        powerful_list = self.conf.meta_demon_config.powerful_list_v
        return self.cur_boss_type is not None and self.cur_boss_type in powerful_list

    def before_run(self):
        # 导航阶段的未知页兜底（goto_page 内部时任务层 run() 的 case _ 轮不到）：
        # 1) 昨日狩猎奖励面板——没有红 X，唯一出口是「领取」，必须先于红 X 判断
        # 2) 礼包/公告类全屏弹窗——点右上角红 X
        self.navigator.add_unknown_closer(self._claim_yesterday_reward, self._close_unexpected_popup)
        self.conf = self.config.meta_demon
        self.limit_time = self.conf.meta_demon_config.limit_time_v
        self.limit_count = self.conf.meta_demon_config.limit_count
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

    def run(self):
        self.before_run()
        self.goto_page(ipages.page_meta_demon_boss)
        while True:
            self.update_global_state()
            if self.State.done:
                break
            self.screenshot()
            # 领取奖励面板会盖住 page_meta_demon 的检查位，页识别必然失败；
            # 放在判页之前处理，避免被当成未知页交给返回键空转
            if self._claim_yesterday_reward():
                continue
            current_page = self.get_current_page()
            match current_page:
                case ipages.page_meta_demon:
                    self.goto_page(ipages.page_meta_demon_boss)
                case ipages.page_meta_demon_boss:
                    if self.check_and_prepare_battle():
                        self.start_battle()
                case ipages.page_battle:
                    self.battle_wait(self.conf.general_battle.random_click_swipt_enable)
                case ipages.page_reward | ipages.page_battle_result:
                    self.click(ipages.random_click(), interval=0.6)
                case _:
                    # 判页失败大多是意外弹窗（礼包/公告/商城推送）盖住了界面。
                    # 先点右上角红 X 把界面捞回来，否则会一直空转到 device 的
                    # 卡死保护，45 秒后抛 GameStuckError 重启。
                    if not self._close_unexpected_popup():
                        time.sleep(random.uniform(0.7, 1.4))
        self.set_next_run(task="MetaDemon", success=True)
        self.finish_task()

    def start_battle(self):
        logger.hr('点击挑战，开始战斗')
        # 有界：好友副本被打死、或协助次数到顶时挑战按钮会消失，
        # 无界 while 会一路空转到 device 层 45 秒卡死保护而重启
        timer = Timer(20).start()
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                self.total_count += 1
                self.run_general_battle(
                    self.conf.switch_soul.get_general_battle_conf(self.cur_boss_type),
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

    def update_global_state(self):
        if datetime.now() - self.start_time >= self.limit_time or \
                self.total_count >= self.limit_count:
            self.State.done = True

    def check_and_prepare_battle(self) -> bool:
        """检查并做战斗前的准备工作
        :return: True可以战斗, False不能战斗
        """
        logger.hr('战斗准备', 2)
        if not self.check_fatigue():
            return False
        # 好友分享的可战斗副本优先级最高：命中就跳过合成、开自己队列、切御魂，
        # 直接打好友那只（协助次数上限由 start_battle 的超时兜底）
        if self.check_and_switch_friend_boss():
            powerful = '开启' if self.appear(self.I_MD_DISABLE_POWERFUL_FIRE, interval=0.4) else '关闭'
            logger.info(f'好友副本：沿用当前御魂与强力追击设置（强力{powerful}），不切换')
            return True
        self.synthesis_boss_ticket()
        return self.check_and_switch_ticket() and self.check_and_switch_soul() and self.check_and_switch_powerful()

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
            self.do_synthesis(type_ticket_dict[boss_type][0], type_ticket_dict[boss_type][1])
        # 关闭合成弹窗退回到鬼王界面。注意礼包/商城类弹窗是半透明的，底部「式神录」
        # 按钮会透出来（22:33 崩溃帧实测 0.947 HIT），所以不能拿它当「已回到鬼王界面」
        # 的判据——否则会带着弹窗继续往下走，紧接着召唤入口 15 秒 0 命中。
        # 改成「点 X 直到 X 消失」，以弹窗自身为判据。
        if self.appear(self.I_MD_CLOSE_POPUP, interval=0.6):
            self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6, timeout=8)

    def do_synthesis(self, target_boss_ticket: RuleImage, need_ticket: int):
        """开始合成鬼王
        :param target_boss_ticket: 目标鬼王级别门票
        :param need_ticket: 合成当前级别鬼王需要的门票数量
        """
        if not self.appear(target_boss_ticket):
            logger.info(f'未识别到门票素材 {target_boss_ticket}，跳过该星级合成')
            return
        click_cnt, max_cnt = 0, random.randint(2, 3)
        while True:
            self.screenshot()
            if click_cnt >= max_cnt:  # 兜底,多次点击开始合成都没有出现获得奖励则退出
                break
            if self.appear(self.I_MD_SYNTHESIS_NEED_MONEY, interval=0.8):  # 出现花钱购买合成券
                self.ui_click(self.I_MD_CLOSE_POPUP, self.I_MD_START_SYNTHESIS, interval=0.6)  # 关闭购买弹窗
                break
            if self.appear(self.I_UI_REWARD, interval=0.8):  # 出现获得奖励说明合成成功
                self.click(ipages.random_click(), interval=0.6)
                click_cnt = 0  # 清空点击标志
                continue
            if self.appear(self.I_MD_SYNTHESIS_EMPTY, interval=0.8):  # 有空位
                empty_list = self.I_MD_SYNTHESIS_EMPTY.match_all(self.device.image, frame_id=self.device.image_frame_id)
                if len(empty_list) < need_ticket:  # 空位数量小于需要的门票数量->门票不够则退出
                    logger.info(f'{target_boss_ticket} 空位不足，跳过该星级合成')
                    break
            if self.appear_then_click(target_boss_ticket, interval=2):  # 点击对应级别鬼王门票
                if self.appear_then_click(self.I_MD_START_SYNTHESIS, interval=2.5):  # 点击开始合成
                    click_cnt += 1
                continue
        logger.info(f'{target_boss_ticket} 合成完成')

    def check_fatigue(self):
        logger.hr('检查疲劳度')
        current, remain, total = self.O_MD_FATIGUE.ocr_digit_counter(self.device.image)
        # 不喝茶且当前疲劳度已满
        if not self.conf.meta_demon_config.auto_tea and (current > total or remain < 0):
            self.set_next_run('MetaDemon', target=datetime.now() + self.conf.scheduler.wait_interval)
            self.finish_task()
        return True

    def _meta_page_visible(self) -> bool:
        """超鬼王主界面/鬼王界面是否已恢复（弹窗是否已彻底关掉）。"""

        return (
            self.appear(GameUiAssets.I_PAGE_METADEMON_MD_CHECK_MAIN_PAGE, interval=0.3)
            or self.appear(GameUiAssets.I_PAGE_METADEMON_CHECK_BOSS_PAGE, interval=0.3)
        )

    def _claim_yesterday_reward(self) -> bool:
        """处理进入超鬼王时弹出的「昨日狩猎奖励」面板。

        实测（2026-11-05 10:45 / 10:48 三次同帧崩溃）：
        - 面板盖住右上检查位，page_meta_demon 只有 0.254（阈值 0.8），页识别必然失败；
        - 面板没有红 X（I_MD_CLOSE_POPUP 只有 0.430），唯一出口是下方「领取」；
        - 旧素材命中率只有 0.342（旧版按钮），所以「点返回键空转到
          GameTooManyClickError」是必然结局。重裁后 226x55 素材实测 1.000，
          178 张快照里只有这 3 帧命中、其余最高 0.437。
        - 「领取」按钮只在面板态出现，消失即代表面板已关（坑 37：判据不能选两态都出现的素材）。

        点击后不自己扛完整个流程：奖励页由现成的 page_reward 页定义 +
        `reward_random_click()` 边收尾（task_pages.py `page_reward->page_meta_demon_boss`），
        本函数只在「领取消失但页面还没回来」时补点空白，最多 2 次。
        """

        now = time.time()
        if now < self._reward_fail_until:
            return False

        # 阶段一：面板在 → 点「领取」
        if self.appear(self.I_MD_GET_YESTERDAY_REWARD, interval=0.4):
            logger.info('识别到「昨日狩猎奖励」面板，点击领取')
            # 复用现成的有界点击器：最多 3 次、12 秒、连续 2 帧确认消失。
            # 不用固定 sleep 判完成，避免奖励页推开时的半渲染帧被当成"已结束"（坑 35）。
            done = self.ui_click_until_disappear(
                self.I_MD_GET_YESTERDAY_REWARD,
                interval=1.2,
                timeout=12,
                max_clicks=3,
                confirm_frames=2,
            )
            self._reward_claim_ts = time.time()
            self._reward_blank_cnt = 0
            if not done:
                # 再点下去会撞 device 层「同按钮 10 次」的 GameTooManyClickError
                logger.error('「领取」点击后 12 秒内面板仍未关闭，停手并冷却 10 分钟')
                self._reward_fail_until = time.time() + 600
            return True
        # 阶段二：领取已点过、面板消失，但页面还没恢复——奖励页点空白收尾
        if self._reward_claim_ts and now - self._reward_claim_ts < 10:
            if self._meta_page_visible():
                self._reward_claim_ts = 0.0
                return False
            if self._reward_blank_cnt < 2:
                self._reward_blank_cnt += 1
                logger.info(f'奖励页点空白关闭（第 {self._reward_blank_cnt} 次）')
                self.click(ipages.random_click(), interval=0.8)
                time.sleep(1.2)
                return True
        elif self._reward_claim_ts:
            # 10 秒窗口过期仍未确认：清零，等下一次真正弹面板时重新计
            self._reward_claim_ts = 0.0
        return False

    def _close_unexpected_popup(self) -> bool:
        """关闭遮挡界面的意外弹窗（礼包、公告、商城推送等），点右上角红 X。

        素材复用弹窗通用关闭按钮 I_MD_CLOSE_POPUP。实测：
        - 「宠梦礼包」全屏弹窗 0.925（命中点 1201,116，就在 X 上）
        - 庭院 / 探索 / 鬼王主界面 / 活动页 全部 ≤ 0.505，不会误点
        只在确实匹配到 X 时才动作（阈值 0.8），并等一小段淡出时间。
        同一时间窗内最多连点 5 次：再点下去会触发 device 层同位置 10 次的
        GameTooManyClickError 重启，那是更差的结果。
        """
        if not self.appear(self.I_MD_CLOSE_POPUP, interval=0.4):
            self._popup_close_cnt = 0
            return False
        now = time.time()
        if now - getattr(self, '_popup_close_last', 0.0) > 60:
            self._popup_close_cnt = 0
        if getattr(self, '_popup_close_cnt', 0) >= 5:
            return False
        self._popup_close_cnt = getattr(self, '_popup_close_cnt', 0) + 1
        self._popup_close_last = now
        logger.warning('识别到遮挡界面的意外弹窗，点击右上角红 X 关闭')
        self.appear_then_click(self.I_MD_CLOSE_POPUP, interval=0.6)
        time.sleep(1.2)  # 等弹窗淡出，避免下一帧判到半透明残影
        return True

    @staticmethod
    def _ocr_seconds(text) -> int | None:
        """把 OCR 出来的「01:28:00」「01:28:00后逃跑」解析成秒数，失败返回 None。"""
        if not text:
            return None
        seconds = int(Duration.parse_time(str(text)).total_seconds())
        return seconds if seconds > 0 else None

    def _is_viewing_friend_boss(self) -> bool | None:
        """当前界面显示的鬼王是不是好友分享的那一只。

        判据：中央「XX:XX:XX 后逃跑」与左侧好友卡片「剩余时间」是同一个计时器，
        两者相等（±2 秒，两次 OCR 之间有耗时）就说明中央显示的是好友的鬼王。
        实测（2026-11-04）：已切好友 01:28:00 / 01:28:00 相等；
        自家的 01:28:05 / 00:58:48 相差 29 分钟，不会误判。
        任一 OCR 失败返回 None，由调用方降级处理。
        """
        friend = self._ocr_seconds(self.O_MD_FRIEND_REMAIN.ocr(self.device.image))
        escape = self._ocr_seconds(self.O_MD_BOSS_ESCAPE.ocr(self.device.image))
        if friend is None or escape is None:
            logger.warning(f'好友副本倒计时识别失败（剩余时间={friend}，逃跑倒计时={escape}）')
            return None
        logger.info(f'倒计时比对：好友剩余 {friend}s / 中央逃跑 {escape}s')
        return abs(friend - escape) <= 2

    def check_and_switch_friend_boss(self) -> bool:
        """好友分享的可战斗副本优先：发现即切过去直接打。

        实测（2026-11-04，1280x720 四张帧）：
        - I_MD_FRIEND_SHARED：有分享 0.991~0.993，无分享「暂无好友发现鬼王」0.154；
        - 自家鬼王召唤后与好友副本**都显示「挑战」**（I_MD_FIRE 同坐标 0.998），
          所以 I_MD_FIRE 不能当「切换成功」的判据，只能靠倒计时比对；
        - 好友鬼王星级在界面上不可见，因此不切御魂；也不能按 powerful_list 决定
          强力追击 —— check_and_switch_powerful 里的 ui_click 是无界循环，
          素材不匹配时会死循环，好友模式一律沿用当前设置。
        """
        self.screenshot()
        if time.time() < self._friend_block_until:
            return False
        if not self.appear(self.I_MD_FRIEND_SHARED, interval=0.6):
            return False
        if self._is_viewing_friend_boss():
            logger.info('当前已在好友分享的副本内，直接挑战')
            self._friend_mode = True
            return True
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

    def check_and_switch_ticket(self):
        """切换鬼王门票并更新当前鬼王级别"""
        logger.hr('召唤鬼王')
        # 打开切换门票弹窗。「全部队列」是开关按钮：点一次开、再点一次就关，
        # 所以不能每轮都点。策略是「点一次 -> 等淡入 -> 只判定不点击」，
        # 只有点击疑似被吞（5 秒内毫无反应）才补点第二次，最多 2 次。
        timer = Timer(15).start()
        click_cnt = 0
        next_click_at = 0.0
        close_at = 0.0
        closed_cnt = 0
        opened = False
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_MD_FIRE, interval=1.2):  # 可以直接挑战鬼王则退出
                return True
            if self._popup_opened():  # 召唤队列弹窗已稳定打开（有票或无票）
                opened = True
                break
            if click_cnt < 2 and timer.current() >= next_click_at:
                if self.appear_then_click(self.I_MD_SWITCH_TICKET, interval=0.6):
                    click_cnt += 1
                    next_click_at = timer.current() + 5.0
                    time.sleep(1.0)  # 等弹窗淡入完成，避免把过渡帧误判成未打开
                    continue
            # 入口按钮点不到、召唤队列弹窗也没打开：多半是被礼包/公告之类的
            # 意外弹窗整屏盖住（实测「宠梦礼包」会让 I_MD_SWITCH_TICKET 全程 0 命中）。
            # 每 4 秒尝试点一次右上角红 X，把界面捞回来再继续。
            if timer.current() >= close_at:
                close_at = timer.current() + 4.0
                if self._close_unexpected_popup():
                    closed_cnt += 1
                    next_click_at = 0.0  # 界面已恢复，立刻重新尝试点召唤入口
        if not opened:
            logger.warning(f'15 秒内未能打开召唤队列弹窗（已点 {click_cnt} 次、'
                           f'关闭意外弹窗 {closed_cnt} 次），跳过本轮')
            return False
        # 弹窗已打开，先判无票（「暂无裂变之眼」），有票才继续选卡
        if self.appear(self.I_MD_TICKET_EMPTY, interval=0.6):
            if self._try_refill_ticket():  # 掉落进度未满则委派刷本任务
                return False
            self.State.done = True
            logger.info('没有任何鬼王门票，结束任务')
            return False
        type_ticket_dict: dict[BossType, RuleImage] = {
            BossType.ONE_STAR: self.I_MD_ONE_STAR,
            BossType.TWO_STARS: self.I_MD_TWO_STARS,
            BossType.THREE_STARS: self.I_MD_THREE_STARS,
            BossType.FOUR_STARS: self.I_MD_FOUR_STARS,
            BossType.FIVE_STARS: self.I_MD_FIVE_STARS,
            BossType.SIX_STARS: self.I_MD_SIX_STARS,
        }
        # 获取所有存在的鬼王门票
        can_fire_list = [boss_type for boss_type, ticket_rule_image in type_ticket_dict.items() if
                         self.appear(ticket_rule_image)]
        if len(can_fire_list) <= 0:
            # 弹窗淡入时可能「立即召唤」已出、门票卡片尚未渲染，此时直接判无票会把还剩下的票丢掉；
            # 先给 3 秒重试窗口，把半渲染帧和真无票区分开
            retry_timer = Timer(3).start()
            while not retry_timer.reached():
                time.sleep(0.4)
                self.screenshot()
                can_fire_list = [boss_type for boss_type, ticket_rule_image in type_ticket_dict.items()
                                 if self.appear(ticket_rule_image)]
                if can_fire_list:
                    logger.info(f'重试后识别到可召唤门票：{boss_cn(can_fire_list[0])}鬼王')
                    break
        if len(can_fire_list) <= 0:
            # 与 I_MD_TICKET_EMPTY 分支同属「无票」，同样应尝试补票（掉落进度未满时委派刷本任务）
            if self._try_refill_ticket():
                return False
            self.State.done = True
            logger.info('没有剩余可召唤的鬼王门票，结束任务')
            if self._popup_opened():
                self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6)
            return False
        # 根据存在的鬼王门票和配置的鬼王序列过滤出需要召唤的鬼王门票
        need_fire_list = [boss_type for boss_type in self.conf.meta_demon_config.fire_sequence_v if
                          boss_type in can_fire_list]
        if len(need_fire_list) <= 0:
            self.State.done = True
            logger.info('现有门票与配置的攻击顺序无交集，没有需要攻击的鬼王，结束任务')
            self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6)
            return False
        # 第一次进来或上次一鬼王类型与这一次不同, 则需要切换御魂和预设
        if self.cur_boss_type is None or self.cur_boss_type != need_fire_list[0]:
            if not self.conf.switch_soul.switch_once:
                self.State.switch_soul_done = False
            self.current_count = 0
        self.cur_boss_type = need_fire_list[0]  # 更新当前鬼王级别
        target_ticket_rule_image = type_ticket_dict[self.cur_boss_type]
        selected = False
        logger.info(f'召唤{boss_cn(self.cur_boss_type)}鬼王')
        # 有界退出：门票图标失效或召唤无响应时不能无限空转（会绕过 run() 的限时检查）
        timer = Timer(30).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_MD_FIRE):  # 可以挑战鬼王则退出
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

    def _popup_opened(self) -> bool:
        """「裂变之眼召唤队列」弹窗是否已稳定打开。

        有票时弹窗底部是「立即召唤」，无票时是「暂无裂变之眼」+「前往获取」，
        两者任一命中都说明弹窗已经开出来了。
        """
        return (self.appear(self.I_MD_GET_BOSS, interval=0.6)
                or self.appear(self.I_MD_TICKET_EMPTY, interval=0.6))

    def _close_summons_queue(self):
        """关闭召唤队列弹窗，失败不阻塞后续流程。"""
        if self._popup_opened():
            self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6, timeout=8)

    def _read_drop_progress(self) -> tuple[int, int] | None:
        """读取今日掉落进度，返回 (已掉落, 上限)。

        上限恒为 50，但 OCR 仍可能误读，故要求 0 < total 且 0 <= current <= total，
        不满足就重截图重试，最多 3 次；全部失败返回 None（调用方回退原逻辑）。
        """
        for attempt in range(3):
            current, _, total = self.O_MD_TICKET.ocr_digit_counter(self.device.image)
            if 0 < total and 0 <= current <= total:
                if total != 50:
                    logger.warning(f'掉落进度上限识别为 {total}，与预期 50 不符')
                logger.info(f'掉落进度 {current}/{total}')
                return current, total
            logger.warning(f'掉落进度识别异常，第 {attempt + 1}/3 次重试（已掉落={current}，上限={total}）')
            time.sleep(0.5)
            self.screenshot()
        return None

    def _refill_task(self) -> str | None:
        """返回配置的补票任务；未配置或调度器未启用返回 None。"""
        task = self.conf.meta_demon_config.refill_task_v
        if task is None:
            return None
        item = getattr(self.config.model, convert_to_underscore(task), None)
        if item is None or not item.scheduler.enable:
            logger.warning(f'补票任务 {task} 已配置，但该任务的调度器未启用，跳过')
            return None
        return task

    def _try_refill_ticket(self) -> bool:
        """今日掉落进度未满时委派补票任务，返回 True 表示已委派。

        调用前召唤队列弹窗必须已经打开：(x/50) 只在这个弹窗里出现。
        """
        progress = self._read_drop_progress()
        if progress is None:
            logger.warning('掉落进度无法识别，回退为按原逻辑退出')
            self._close_summons_queue()
            return False
        current, total = progress
        if current >= total:
            logger.info(f'今日掉落已达上限（{current}/{total}），无需补票')
            self._close_summons_queue()
            return False
        task = self._refill_task()
        if task is None:
            logger.info(f'今日掉落未满（{current}/{total}），但没有可用的补票任务')
            self._close_summons_queue()
            return False
        logger.info(f'今日掉落未满（{current}/{total}），转去执行补票任务 {task}')
        self._close_summons_queue()  # 先关弹窗再交棒，避免收尾导航被弹窗挡住
        self.config.task_call(task)
        self.State.done = True
        return True

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

    def _is_meta_demon_boss_page(self) -> bool:
        """鬼王页判据：召唤鬼王 / 切换门票 / 挑战按钮任一出现。"""
        return (self.appear(self.I_MD_SUMMON_BOSS)
                or self.appear(self.I_MD_SWITCH_TICKET)
                or self.appear(self.I_MD_FIRE))

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
            if self._is_meta_demon_boss_page():
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

    def check_and_switch_soul(self) -> bool:
        """切换御魂：从鬼王页进式神录，换完按返回，不交给导航器绕路。"""
        if self.State.switch_soul_done:
            return True
        logger.hr('切换御魂')
        self.State.switch_soul_done = True
        if not self._enter_shikigami_records():
            # 点不到「式神录」时退回导航器；此时图上只剩真实边 鬼王页 -> 式神录
            self.goto_page(ipages.page_shikigami_records)
        if self.conf.switch_soul.switch_once:  # 只切换一次则将配置的鬼王御魂全部切换
            for boss_type in self.conf.meta_demon_config.fire_sequence_v:
                switch_type, (group, team) = self.conf.switch_soul.get_switch_by_enum(boss_type)
                if switch_type is None:
                    continue
                if switch_type == 'int':
                    self.run_switch_soul((group, team))
                if switch_type == 'str':
                    self.run_switch_soul_by_name(group, team)
            self._leave_shikigami_records()
            return True
        # 当前鬼王类型未知, 无法切换御魂
        if self.cur_boss_type is None:
            logger.error('当前鬼王类型为空，无法切换御魂')
            self._leave_shikigami_records()
            return False
        switch_type, (group, team) = self.conf.switch_soul.get_switch_by_enum(self.cur_boss_type)
        if switch_type is None:
            logger.error(f'{boss_cn(self.cur_boss_type)}鬼王的御魂配置格式无效')
            self._leave_shikigami_records()
            return False
        if switch_type == 'int':
            self.run_switch_soul((group, team))
        if switch_type == 'str':
            self.run_switch_soul_by_name(group, team)
        self._leave_shikigami_records()
        return True

    def finish_task(self):
        self.finish_task_navigation()
        raise TaskEnd


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()
