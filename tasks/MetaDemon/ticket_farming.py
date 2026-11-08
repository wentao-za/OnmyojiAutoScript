# This Python file uses the following encoding: utf-8
"""超鬼王门票域：前置刷票 + 反应式补票 + 掉落进度判定。

职责边界
--------
- **怎么把票刷出来**：前置刷取（觉醒七层 / 探索），复用 EvoZone / Exploration
  的既有战斗流程，只把御魂、战斗参数与限时从超鬼王配置注入。
- **没票了怎么办**：反应式补票 —— 读召唤队列弹窗里的掉落进度 (x/50)，
  未满则委派 `refill_task` 指定的任务（D5：与前置刷票共存，前置优先）。
- **不含**选卡召唤（`boss_battle.summon_and_fire`）与主动分享（`assistance`）。

与 self 版的两处关键差异（U1：self 不保证正确，均已回 dev 实测）
------------------------------------------------------------
1. self 只在 `AwakeTicketTask` 上覆盖 `_config`，但 `EvoZone.ScriptTask.run_alone()`
   全程读 `self.config.evo_zone.evo_zone_config.layer` / `.general_battle_config`，
   `_config` **从未被读取** —— 结果是仍然按用户自己 EvoZone 任务的层数与战斗参数跑，
   而不是门票刷取要求的觉醒七层。这里改用 `_TaskConfigProxy` 在 `self.config`
   这一层做替换，两条读法同时生效，且不污染用户配置（不写回 JSON）。
2. self 的 `metademon_tickets_full` 依赖 `GeneralBattleAssets.O_METADEMON_PASS`，
   test 副本缺该素材（dev 有），已按 U3 从 dev 同步 `GeneralBattle/assets.py`。
"""
import re
import time
from enum import Enum

from cached_property import cached_property

from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger
from module.config.utils import convert_to_underscore
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.EvoZone.config import EvoZone, EvoZoneConfig, Layer, UserStatus as AwakeUserStatus
from tasks.EvoZone.script_task import ScriptTask as AwakeTask
from tasks.Exploration.config import (
    Exploration,
    ExplorationConfig,
    AutoRotate,
    UpType,
    UserStatus as ExplorationUserStatus,
)
from tasks.Exploration.script_task import ScriptTask as ExplorationTask
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.default_pages import page_reward
import tasks.GameUi.task_pages as pages

from tasks.MetaDemon.assets import MetaDemonAssets
from tasks.MetaDemon.config import ExplorationTarget, MetaDemonMode
from tasks.MetaDemon.md_ui import MdUiMixin


TICKET_MODES = (MetaDemonMode.AWAKE, MetaDemonMode.EXPLORATION)


# --------------------------------------------------------------------- 满票检测


class TicketsFull(Exception):
    """奖励页门票已满，立即展开调用栈以阻止下一场战斗。"""


def metademon_tickets_full(task) -> bool:
    """读取当前奖励截图的门票/上限；无有效上限时不判满。"""
    # I_REWARD is the red-daruma settlement image; I_UI_REWARD is the
    # separate "获得奖励" banner and does not identify this battle reward screen.
    if not task.appear(GeneralBattleAssets.I_REWARD):
        return False
    current, remaining, total = GeneralBattleAssets.O_METADEMON_PASS.ocr_digit_counter(task.device.image)
    if total <= 0 or current < 0:
        logger.warning('超鬼王门票 OCR 未识别到有效计数')
        return False
    logger.info(f'超鬼王门票: {current}/{total}')
    return current >= total


class TicketRewardMixin:
    """在「每场战斗结算」与「导航进入奖励页」两处插入满票检测。

    刷满票后必须立刻停止，否则会白打一场；用异常而不是返回值，
    因为这两处都是别人（Exploration / GeneralBattle / 导航器）的调用栈。
    """

    def check_ticket_reward(self):
        if getattr(self, '_ticket_farming_complete', False):
            return
        if metademon_tickets_full(self):
            logger.info('门票已满')
            raise TicketsFull

    def _handle_reward(self, context, config):
        self.check_ticket_reward()
        return super()._handle_reward(context, config)

    def install_ticket_reward_hook(self):
        reward = self.navigator.resolve_page(page_reward)
        if reward is None:
            logger.warning('奖励页未解析到 session 页面，跳过导航级满票检测')
            return
        # 导航进入奖励页时也必须先 OCR，再执行原来的结算点击。
        reward.on_enter_success.insert(0, lambda task: task.check_ticket_reward())


class AwakeTicketTask(TicketRewardMixin, AwakeTask):
    """觉醒七层刷票。

    `run_alone()` 内部读 `self.config.evo_zone`，所以真正的配置替换由
    `_TaskConfigProxy` 在 `self.config` 层完成；这里只需保证 `_config`
    与代理暴露的是同一个对象（`run_ticket_farming` 会显式用它取御魂配置）。
    """

    @cached_property
    def _config(self):
        return self.config.evo_zone

    def _infer_task_category(self):
        return 'evo_zone'


class ExplorationTicketTask(TicketRewardMixin, ExplorationTask):
    """探索刷票。`BaseExploration` 全流程读 `_config`，代理与 `_config` 同时生效。"""

    @cached_property
    def _config(self):
        return self.config.exploration

    def _infer_task_category(self):
        return 'exploration'

    @property
    def exp_page_handle_dict(self):
        handlers = super().exp_page_handle_dict
        original = handlers.get(page_reward)
        if original is None:
            logger.warning('探索页处理表里没有 page_reward，跳过该页的满票检测')
            return handlers

        def reward():
            self.check_ticket_reward()
            return original()

        handlers[page_reward] = reward
        return handlers

    def activate_realm_raid(self, con_scrolls, con, current_page):
        """门票刷取不检测结界票，也不触发个人突破调度。"""
        return


# ----------------------------------------------------------------- 配置适配


def ticket_farming_config(meta, mode):
    """仅把超鬼王专用字段适配为复用流程需要的设置，不读取独立任务配置。"""
    common = meta.general_config
    soul = meta.switch_soul_config.switch_for('ticket_farming')
    battle = meta.ticket_farming_battle_conf
    if mode == MetaDemonMode.AWAKE:
        return EvoZone(
            evo_zone_config=EvoZoneConfig(user_status=AwakeUserStatus.ALONE, layer=Layer.SEVEN,
                limit_time=meta.meta_demon_config.limit_time, limit_count=2 ** 31 - 1,
                soul_buff_enable=common.awake_buff_enable),
            switch_soul_config=soul, general_battle_config=battle)
    if mode == MetaDemonMode.EXPLORATION:
        target = {ExplorationTarget.ALL: UpType.ALL, ExplorationTarget.COIN: UpType.COIN,
                  ExplorationTarget.EXP: UpType.EXP, ExplorationTarget.DARUMA: UpType.DARUMAA}
        return Exploration(
            exploration_config=ExplorationConfig(user_status=ExplorationUserStatus.ALONE,
                limit_time=meta.meta_demon_config.limit_time, minions_cnt=2 ** 31 - 1,
                auto_rotate=AutoRotate.yes if common.exploration_rotate else AutoRotate.no,
                up_type=target[common.exploration_target]),
            switch_soul_config=soul, general_battle_config=battle)
    raise ValueError(f'未知门票刷取模式: {mode}')


class _TaskConfigProxy:
    """把 `.evo_zone` / `.exploration` 换成门票刷取用的适配配置，其余属性透传。

    为什么需要这层（U1 实测）：
    - `EvoZone.ScriptTask.run_alone()` 读 `self.config.evo_zone.evo_zone_config.layer`、
      `self.config.evo_zone.general_battle_config`；
    - `BaseExploration.pre_process()` 读 `self.config.exploration.exploration_config`。

    这两处都绕过 `_config`，所以只在子类覆盖 `_config` 是无效的。
    而直接改 `config.model.evo_zone` 会污染用户配置（`save()` 时写回 JSON），
    故用代理隔离：读走适配值，写与其余属性一律转发给真配置。
    """

    def __init__(self, config, **overrides):
        object.__setattr__(self, '_config', config)
        object.__setattr__(self, '_overrides', overrides)

    def __getattr__(self, name):  # 仅在实例/类属性都找不到时触发
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, '_config'), name)

    def __setattr__(self, name, value):
        overrides = object.__getattribute__(self, '_overrides')
        if name in overrides:
            overrides[name] = value
            return
        setattr(object.__getattribute__(self, '_config'), name, value)


# --------------------------------------------------------------- 前置刷票入口


def tickets_full_before_farming(owner) -> bool:
    """刷取前先在探索入口读一次门票，已满就不必白跑一趟。"""
    owner.goto_page(pages.page_exploration)
    for _ in range(3):
        owner.screenshot()
        if not owner.appear(GameUiAssets.I_CHECK_EXPLORATION):
            continue
        current, _, total = MetaDemonAssets.O_METADEMON_PASS_EXP.ocr_digit_counter(owner.device.image)
        if current >= 0 and total > 0:
            logger.info(f'门票刷取前检测: {current}/{total}')
            return current >= total
    logger.warning('探索入口未识别到有效超鬼王门票计数，继续刷取并由奖励页检测满票')
    return False


def run_ticket_farming(owner, mode) -> bool:
    """前置刷票；御魂和战斗参数直接使用超鬼王门票刷取配置。

    :return: True 表示门票已满、无需再刷（调用方可以直接进战斗）。
    """
    if mode not in TICKET_MODES:
        raise ValueError(f'未知门票刷取模式: {mode}')
    if tickets_full_before_farming(owner):
        logger.info('超鬼王门票已满，跳过门票刷取')
        return True

    config = owner.config
    meta = config.meta_demon
    limit_time = meta.meta_demon_config.limit_time_v
    adapted = ticket_farming_config(meta, mode)
    if mode == MetaDemonMode.AWAKE:
        runner = AwakeTicketTask(_TaskConfigProxy(config, evo_zone=adapted), owner.device)
    else:
        runner = ExplorationTicketTask(_TaskConfigProxy(config, exploration=adapted), owner.device)

    runner.start_time = owner.start_time
    runner.current_count = 0
    runner.limit_count = 2 ** 31 - 1
    runner.install_ticket_reward_hook()
    buff_opened = False
    try:
        if mode == MetaDemonMode.AWAKE:
            soul = runner._config.switch_soul_config
            if soul.enable or soul.enable_switch_by_name:
                runner.goto_page(pages.page_shikigami_records)
                if soul.enable_switch_by_name:
                    runner.run_switch_soul_by_name(soul.group_name, soul.team_name)
                else:
                    runner.run_switch_soul(soul.switch_group_team)
            if meta.general_config.awake_buff_enable:
                runner.goto_page(pages.page_main)
                runner.open_buff()
                runner.awake(is_open=True)
                buff_opened = True
                runner.close_buff()
            runner.limit_time = limit_time
            runner.run_alone()
        else:
            runner.pre_process()
            runner.limit_time = limit_time
            runner.exec_exp_page()
    except TicketsFull:
        return True
    except TaskEnd:
        # 复用流程里任何一环认为本轮结束，都不该把整条调用栈炸掉
        logger.info('门票刷取流程自行结束')
        return True
    finally:
        runner._ticket_farming_complete = True
        if buff_opened:
            runner.goto_page(pages.page_main)
            runner.open_buff()
            runner.awake(is_open=False)
            runner.close_buff()
    return False


# --------------------------------------------------------------- 反应式补票


class TicketStatus(str, Enum):
    """`ensure_ticket()` 的三态结果。"""

    READY = 'ready'      # 眼前已有可直接挑战的鬼王（挑战按钮在），跳过召唤
    SUMMON = 'summon'    # 召唤队列弹窗已开且有票，交给 summon_and_fire 选卡
    BLOCKED = 'blocked'  # 本轮不能继续（开不出弹窗 / 已委派补票 / 已无票结束）


class DropStatus(str, Enum):
    """`judge_drop_progress()` 的三态结果。"""

    NOT_FULL = 'not_full'      # 识别到 N/M 且 N < M，还有掉落余量
    EXHAUSTED = 'exhausted'    # N >= M，或进度区域连续空白（补票用尽）
    UNKNOWN = 'unknown'        # 非空但读不懂：识别失败，绝不能当用尽处理


class RefillOutcome(str, Enum):
    """`try_refill_ticket()` 的三态结果。"""

    DELEGATED = 'delegated'    # 已委派补票任务
    EXHAUSTED = 'exhausted'    # 补票用尽（掉落已满 / 空白确认 / 未配补票任务）
    UNKNOWN = 'unknown'        # 掉落进度识别失败，保守按原逻辑结束，不驻留


class TicketFarmingMixin(MdUiMixin):
    """召唤队列弹窗的开窗、判空与补票委派。

    前置契约：本 mixin 只负责「弹窗开没开、有没有票」，
    选卡与点「立即召唤」属于 `boss_battle.summon_and_fire`。
    """

    def ensure_ticket(self) -> TicketStatus:
        """打开「裂变之眼召唤队列」弹窗并确认里面有票可点。"""
        logger.hr('召唤鬼王')
        # 「全部队列」是开关按钮：点一次开、再点一次就关，所以不能每轮都点。
        # 策略是「点一次 -> 等淡入 -> 只判定不点击」，只有点击疑似被吞
        # （5 秒内毫无反应）才补点第二次，最多 2 次。
        timer = Timer(15).start()
        click_cnt = 0
        next_click_at = 0.0
        close_at = 0.0
        closed_cnt = 0
        opened = False
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_MD_FIRE, interval=1.2):  # 可以直接挑战鬼王则退出
                return TicketStatus.READY
            if self.summon_panel_opened():  # 召唤队列弹窗已稳定打开（有票或无票）
                opened = True
                break
            if click_cnt < 2 and timer.current() >= next_click_at:
                if self.press_summon_entry():
                    click_cnt += 1
                    next_click_at = timer.current() + 5.0
                    time.sleep(1.0)  # 等弹窗淡入完成，避免把过渡帧误判成未打开
                    continue
            # 入口按钮点不到、召唤队列弹窗也没打开：多半是被礼包/公告之类的
            # 意外弹窗整屏盖住（实测「宠梦礼包」会让 I_MD_SWITCH_TICKET 全程 0 命中）。
            # 每 4 秒尝试点一次右上角红 X，把界面捞回来再继续。
            if timer.current() >= close_at:
                close_at = timer.current() + 4.0
                if self.close_unexpected_popup():
                    closed_cnt += 1
                    next_click_at = 0.0  # 界面已恢复，立刻重新尝试点召唤入口
        if not opened:
            logger.warning(f'15 秒内未能打开召唤队列弹窗（已点 {click_cnt} 次、'
                           f'关闭意外弹窗 {closed_cnt} 次），跳过本轮')
            return TicketStatus.BLOCKED
        # 弹窗已打开，先判无票（「暂无裂变之眼」），有票才继续选卡
        if self.appear(self.I_MD_TICKET_EMPTY, interval=0.6):
            # 面板自己写着没有票，属于确定的「空」，必须先置位再交给 exhaust_ticket，
            # 否则它会把这一支当成「召唤超时」直接跳过补票
            self._summon_empty = True
            self.exhaust_ticket()
            return TicketStatus.BLOCKED
        return TicketStatus.SUMMON

    def exhaust_ticket(self) -> None:
        """召唤队列里已无可召唤的票：补票 -> 驻留等待 -> 结束，按现场三选一。

        调用前要求弹窗处于打开态 —— (x/50) 只在这个弹窗里出现。
        `summon_and_fire` 在「没有任何可点卡片」时才置 `_summon_empty`，
        30 秒召唤超时不会走到这里（弹窗与掉落进度可能都还在，不能判死）。
        旁路注意：「有票但不在 fire_sequence」不经过本方法（走驻留或直接 done）。
        """
        if not getattr(self, '_summon_empty', False):
            return
        outcome = self.try_refill_ticket()
        if outcome == RefillOutcome.DELEGATED:
            return
        if outcome == RefillOutcome.EXHAUSTED:
            if self._enter_idle_wait('tickets'):
                return
            logger.info('没有剩余可召唤的鬼王门票，结束任务')
            self.State.done = True
            return
        # UNKNOWN：掉落进度读不懂（乱码 / 面板没截到），用户 2026-11-06 拍板「不驻留」。
        # 不能把识别失败当用尽，也不能拿它去赌驻留能等到票，保守结束本轮。
        logger.warning('掉落进度识别失败，按「识别失败不驻留」处置，结束本轮')
        self.State.done = True

    # ------------------------------------------------------------ 掉落与补票

    def judge_drop_progress(self) -> tuple[DropStatus, int, int]:
        """判定今日掉落进度，返回 (状态, 已掉落, 上限)。

        用 `ocr_single` 取原始文本再分流：`ocr_digit_counter` 对「空白」和
        「乱码」都返回 (0, 0, 0)（sub_ocr.py），二者必须区分 —— 补票次数用尽后
        界面不显示 50/50，x/50 区域直接空白（2026-11-05 实测截图），空白=用尽；
        乱码是识别失败，保守处理。空白判定双保险：连续 2 帧空白 +
        「暂无裂变之眼」可见（防半渲染帧和「面板根本没截到」被误判成空白）。
        current / total 仅在识别成功时有意义。
        """
        blank_cnt = 0
        for attempt in range(3):
            raw = str(self.O_MD_TICKET.ocr_single(self.device.image) or '').strip()
            m = re.search(r'(\d+)\s*/\s*(\d+)', raw)
            if m:
                current, total = int(m.group(1)), int(m.group(2))
                if 0 < total and 0 <= current <= total:
                    if total != 50:
                        logger.warning(f'掉落进度上限识别为 {total}，与预期 50 不符')
                    logger.info(f'掉落进度 {current}/{total}')
                    if current >= total:
                        return DropStatus.EXHAUSTED, current, total
                    return DropStatus.NOT_FULL, current, total
                logger.warning(f'掉落进度数字越界，当乱码处理（文本="{raw}"）')
            elif raw == '':
                blank_cnt += 1
                if blank_cnt >= 2 and self.appear(self.I_MD_TICKET_EMPTY, interval=0.6):
                    logger.info('掉落进度区域连续空白且显示「暂无裂变之眼」，判定今日掉落已用尽')
                    return DropStatus.EXHAUSTED, 0, 0
            else:
                logger.warning(f'掉落进度识别异常，第 {attempt + 1}/3 次重试（文本="{raw}"）')
            time.sleep(0.5)
            self.screenshot()
        return DropStatus.UNKNOWN, -1, -1

    def refill_task(self) -> str | None:
        """返回配置的补票任务；未配置或调度器未启用返回 None。"""
        task = self.conf.meta_demon_config.refill_task_v
        if task is None:
            return None
        item = getattr(self.config.model, convert_to_underscore(task), None)
        if item is None or not item.scheduler.enable:
            logger.warning(f'补票任务 {task} 已配置，但该任务的调度器未启用，跳过')
            return None
        return task

    def try_refill_ticket(self) -> RefillOutcome:
        """按掉落进度决定补票或判用尽，返回三态（见 RefillOutcome）。

        调用前召唤队列弹窗必须已经打开：(x/50) 只在这个弹窗里出现。
        三条返回路径都会先关掉召唤弹窗，调用方无需重复关闭。
        """
        status, current, total = self.judge_drop_progress()
        if status == DropStatus.NOT_FULL:
            task = self.refill_task()
            if task is not None:
                logger.info(f'今日掉落未满（{current}/{total}），转去执行补票任务 {task}')
                self.close_summon_panel()  # 先关弹窗再交棒，避免收尾导航被弹窗挡着
                self.config.task_call(task)
                self.State.done = True
                return RefillOutcome.DELEGATED
            logger.info(f'今日掉落未满（{current}/{total}），但没有可用的补票任务，按补票用尽处理')
            self.close_summon_panel()
            return RefillOutcome.EXHAUSTED
        if status == DropStatus.EXHAUSTED:
            logger.info(f'今日掉落已达上限（{current}/{total}），无需补票')
            self.close_summon_panel()
            return RefillOutcome.EXHAUSTED
        logger.warning('掉落进度无法识别，按识别失败保守处理，不进入驻留')
        self.close_summon_panel()
        return RefillOutcome.UNKNOWN

    # ------------------------------------------------------------ 驻留等待

    def _enter_idle_wait(self, reason: str) -> bool:
        """进入或维持驻留等待；返回 True 表示当前处于驻留态（此时不置 State.done）。

        :param reason: 触发源，'tickets'（票尽且补票用尽）/ 'sequence'（有票但星级
            与攻击顺序无交集）/ 'fatigue'（疲劳满且已开自动喝茶）。
            首次进入的触发源决定复核策略；已在驻留时只收紧截止时刻、不改写触发源，
            否则「票尽」与「疲劳」会互相覆盖，复核时机来回抖动。

        驻留截止 = 下一任务开始前 idle_wait_buffer_min 分钟；没有其他启用任务时
        驻留到本任务 limit_time 为止。轮询、复核与接战由 `run_battle_loop` 的
        idle 分支驱动（`_idle_poll_friend_boss` / `_idle_try_resume`）。
        开关 `idle_wait_enable` 关闭时一律不驻留，三种触发都退回各自的收尾逻辑。
        """
        cfg = self.conf.meta_demon_config
        if not cfg.idle_wait_enable:
            return False
        deadline = self._next_task_start()
        if deadline is None:
            return False
        now_ts = time.time()
        if self._idle_deadline is not None:
            if deadline < self._idle_deadline:
                self._idle_deadline = deadline
            return True
        self._idle_deadline = deadline
        self._idle_reason = reason
        # sequence 复核限频：至少 60 秒才重走一次正常召唤流程，避免高频开关弹窗
        self._idle_next_resume = now_ts + max(60, int(cfg.idle_poll_interval) * 6)
        self._idle_next_deadline_check = now_ts + 30
        logger.info(f'进入驻留等待（触发：{reason}），至 {deadline:%H:%M:%S} 结束')
        return True

    def _exit_idle_wait(self) -> None:
        """退出驻留、回到正常战斗流程。只给有恢复路径的触发源（fatigue / sequence）调用。"""
        logger.info(f'退出驻留等待（原触发：{self._idle_reason}）')
        self._idle_deadline = None
        self._idle_reason = None
        self._idle_next_resume = 0.0
        self._idle_next_deadline_check = 0.0
