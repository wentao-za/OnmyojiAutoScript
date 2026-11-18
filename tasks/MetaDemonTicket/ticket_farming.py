"""鬼王门票刷取，复用觉醒和探索的战斗流程。

本任务是超鬼王门票的**唯一**刷取入口。`tasks/MetaDemon` 只负责「有票之后怎么打」，
战斗里的反应式补票由 `tasks/MetaDemon/queue.py` 判定后把本任务拉起来，形成闭环。
"""
import time
from datetime import datetime

from cached_property import cached_property

from module.cooperation import prepare_battle_cooperation
from module.exception import ScriptError, TaskEnd
from module.logger import logger
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.EvoZone.config import EvoZone, EvoZoneConfig, UserStatus as AwakeUserStatus
from tasks.EvoZone.script_task import ScriptTask as AwakeTask
from tasks.Exploration.config import (
    AutoRotate, Exploration, ExplorationConfig, ExplorationDifficulty, ExplorationLevel,
    UpType, UserStatus as ExplorationUserStatus,
)
from tasks.Exploration.script_task import ScriptTask as ExplorationTask
from tasks.GameUi.default_pages import page_reward
import tasks.GameUi.task_pages as pages
from tasks.MetaDemonTicket.config import TicketMode, ExplorationTarget
from tasks.MetaDemon.assets import MetaDemonAssets
from tasks.GameUi.assets import GameUiAssets


class TicketsFull(Exception):
    """达到单刷或协同满票目标，停止开启下一场战斗。"""


class TicketPartnerFinished(Exception):
    """协同伙伴结束门票阶段。"""


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

    协同侧「对方已结束」也在这里消费。`screenshot()` 被战斗与导航高频调用，
    原实现在那里每帧开一次 sqlite 查库，既拖慢帧率又可能撞上另一实例的写锁；
    现改为每 2 秒查一次、只记 `_peer_signal`，真正的抛出放在
    `check_ticket_reward()` —— 那是结算页与奖励页的稳定时机，抛异常是安全的。
    """

    _PEER_POLL_INTERVAL = 2.0

    _ticket_peer: str = ''
    _ticket_phase_started: float = 0.0
    _ticket_full: bool = False
    _ticket_farming_complete: bool = False
    _peer_signal: str | None = None
    _peer_checked_at: float = 0.0

    def _poll_peer_signal(self):
        """限频查询协同对象的完成状态，只置标志位，不在这里抛异常。"""
        peer = self._ticket_peer
        if not peer or self._ticket_farming_complete or self._peer_signal:
            return
        now = time.time()
        if now - self._peer_checked_at < self._PEER_POLL_INTERVAL:
            return
        runtime = getattr(self.config, 'cooperation_runtime', None)
        if runtime is None:
            return
        self._peer_checked_at = now
        try:
            status = runtime.peer_task_completion('MetaDemonTicket', peer, self._ticket_phase_started)
        except Exception as e:  # 数据库被另一实例短暂锁住时不能让整局崩掉
            logger.warning(f'查询协同对象 {peer} 的完成状态失败：{e}')
            return
        if status == 'tickets_full' and self._ticket_full:
            self._peer_signal = 'tickets_full'
        elif status == 'ticket_finished':
            self._peer_signal = 'ticket_finished'

    def screenshot(self):
        result = super().screenshot()
        if getattr(self, '_battle_context', None) is None:
            self._poll_peer_signal()
        return result

    def check_ticket_reward(self):
        if self._peer_signal == 'tickets_full':
            raise TicketsFull
        if self._peer_signal == 'ticket_finished':
            raise TicketPartnerFinished
        if self._ticket_farming_complete or self._ticket_full:
            return
        if metademon_tickets_full(self):
            logger.info('门票已满')
            peer = self._ticket_peer
            if not peer:
                raise TicketsFull
            self._ticket_full = True
            self.config.cooperation_runtime.notify_task_completion('MetaDemonTicket', peer, 'tickets_full')
            logger.info('本号门票已满，继续组队直到对方满票')

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
    """觉醒刷票。

    `EvoZone.ScriptTask.run_alone()/run_leader()/run_member()` 全程读
    `self.config.evo_zone.*`，**从不读 `_config`**；所以真正的配置替换必须发生在
    `self.config` 这一层，由 `_TaskConfigProxy` 完成。这里让 `_config` 与代理
    暴露的是同一个对象，`run_ticket_farming` 会显式用它取御魂配置。
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


class _TaskConfigProxy:
    """把 `.evo_zone` / `.exploration` 换成门票刷取用的适配配置，其余属性透传。

    为什么需要这层（实测，不是推测）：
    - `EvoZone.ScriptTask.run_alone()` 读 `self.config.evo_zone.evo_zone_config.layer`、
      `self.config.evo_zone.general_battle_config`（19 处），全程绕过 `_config`；
    - `BaseExploration.pre_process()` 读 `self.config.exploration.exploration_config`。

    所以只在子类覆盖 `_config` 是无效的 —— 那会让「觉醒层数 / 麒麟 / 组队身份」
    静默失效，实际按用户自己 EvoZone 任务的配置在跑。直接改
    `config.model.evo_zone` 又会污染用户配置（`save()` 时写回 JSON），故用代理隔离：
    读走适配值，写与其余属性一律转发给真配置。
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



def ticket_farming_config(meta, mode):
    """把本任务的配置适配成觉醒/探索复用流程需要的形状（纯只读，不落盘）。"""
    common = meta.general_config
    soul = meta.switch_soul_config.model_copy()
    battle = meta.general_battle_config
    if mode == TicketMode.AWAKE:
        return EvoZone(
            evo_zone_config=EvoZoneConfig(user_status=AwakeUserStatus(common.user_status.value),
                layer=common.awake_layer, kirin_type=common.awake_kirin_type,
                limit_time=common.limit_time, limit_count=2 ** 31 - 1,
                soul_buff_enable=common.awake_buff_enable),
            switch_soul_config=soul, general_battle_config=battle, invite_config=meta.invite_config)
    if mode == TicketMode.EXPLORATION:
        target = {ExplorationTarget.ALL: UpType.ALL, ExplorationTarget.COIN: UpType.COIN,
                  ExplorationTarget.EXP: UpType.EXP, ExplorationTarget.DARUMA: UpType.DARUMAA}
        if common.exploration_rotate and battle.lock_team_enable:
            battle = battle.model_copy(update={'lock_team_enable': False})
        return Exploration(
            exploration_config=ExplorationConfig(user_status=ExplorationUserStatus(common.user_status.value),
                exploration_level=ExplorationLevel.EXPLORATION_28, difficulty=ExplorationDifficulty.HARD,
                limit_time=common.limit_time, minions_cnt=2 ** 31 - 1,
                auto_rotate=AutoRotate.yes if common.exploration_rotate else AutoRotate.no,
                up_type=target[common.exploration_target]),
            switch_soul_config=soul, general_battle_config=battle, invite_config=meta.invite_config)
    raise ValueError(f'未知门票刷取模式: {mode}')


def tickets_full_before_farming(owner) -> bool:
    """Check the courtyard's exploration landing page before any farming setup."""
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


def _peer_ticket_mode(runtime, peer) -> TicketMode | None:
    """读协同对象的配置，取它的刷票玩法；读不到或取值非法返回 None。

    放在本模块而不是 `module/cooperation.py`：这是超鬼王门票独有的语义
    （两个号必须刷同一种玩法，否则一边觉醒一边探索必然互相空等），
    不属于通用协同框架的职责。
    """
    try:
        data = runtime.read(peer)
    except (ScriptError, OSError, ValueError) as e:
        logger.warning(f'读取协同对象 {peer} 的配置失败：{e}')
        return None
    general = (data.get('meta_demon_ticket') or {}).get('general_config') or {}
    raw = general.get('mode')
    try:
        return TicketMode(raw)
    except ValueError:
        logger.warning(f'协同对象 {peer} 的刷票玩法取值非法：{raw!r}')
        return None


def _resolve_peer(config, meta, mode, runtime) -> tuple[str, bool]:
    """解析本轮的协同对象，并校验双方刷票玩法一致。

    :return: `(peer, ok)`
        - `('', True)` 单刷：身份为单人 / 未配置协同 / 没有可用对端；
        - `(peer, True)` 组队；
        - `(peer, False)` 配了协同但玩法不一致或读不到对方配置 —— 调用方必须
          **整轮跳过**（而不是退化成单刷）：此刻 `prepare` 已经由调度入口做过，
          对端可能已进入「等待队友」状态，单刷会让它空等到 limit_time。
    """
    if meta.general_config.user_status == ExplorationUserStatus.ALONE:
        return '', True
    if runtime is None:
        return '', True
    peer = meta.invite_config.cooperation_script.strip()
    if not peer:
        peer = runtime.peers.get('MetaDemonTicket', '') or runtime.accepted_peer('MetaDemonTicket')
    if not peer:
        return '', True
    other = _peer_ticket_mode(runtime, peer)
    if other is None:
        return peer, False
    if other != mode:
        logger.warning(f'协同对象 {peer} 的刷票玩法为「{other.value}」，'
                       f'与本号「{mode.value}」不一致，为避免双方空等，本轮跳过')
        return peer, False
    return peer, True


def run_ticket_farming(owner, mode) -> bool:
    """刷票主流程。

    :return: True 表示门票已满、无需再刷（调度器按成功间隔排下一次）。
    """
    if mode not in (TicketMode.AWAKE, TicketMode.EXPLORATION):
        raise ValueError(f'未知门票刷取模式: {mode}')

    config = owner.config
    meta = config.meta_demon_ticket
    runtime = getattr(config, 'cooperation_runtime', None)
    peer, consistent = _resolve_peer(config, meta, mode, runtime)
    if not consistent:
        if runtime is not None and peer:
            # 清掉双方的旧唤醒与等待，避免对端按「队友已就位」继续空等
            runtime.skip_without_wake('MetaDemonTicket', peer, 'mode_mismatch')
        return False
    local_full = tickets_full_before_farming(owner)
    if local_full and not peer:
        logger.info('鬼王门票已满，跳过门票刷取')
        return True
    role = meta.general_config.user_status
    limit_time = meta.general_config.limit_time_v
    adapted = ticket_farming_config(meta, mode)
    if mode == TicketMode.AWAKE:
        runner = AwakeTicketTask(_TaskConfigProxy(config, evo_zone=adapted), owner.device)
    else:
        runner = ExplorationTicketTask(_TaskConfigProxy(config, exploration=adapted), owner.device)
    runner.start_time = datetime.now()
    runner.current_count = 0
    runner.limit_count = 2 ** 31 - 1
    if peer:
        # 协同已在调度入口 prepare 过一次，这里只是兜底直接执行（`__main__`）的场景；
        # 调度路径下 owner 带 `_battle_cooperation_prepared=True`，本调用会立即返回。
        prepare_battle_cooperation(owner, 'MetaDemonTicket')
    # 复用流程自己会按 EvoZone / Exploration 的任务名再 prepare 一次，必须挡住，
    # 否则对端会收到第二个任务名的唤醒；同时把房间校验挂到本任务名下。
    runner._battle_cooperation_prepared = True
    runner._battle_cooperation_task = 'MetaDemonTicket'
    runner._ticket_farming_complete = False
    runner._ticket_full = local_full
    runner._ticket_peer = peer
    runner._peer_signal = None
    runner.install_ticket_reward_hook()
    buff_opened = False
    result = False
    try:
        if peer:
            runner._ticket_phase_started = runtime.begin_completion_phase('MetaDemonTicket', peer)
            runtime.peers['MetaDemonTicket'] = peer
            if local_full:
                runtime.notify_task_completion('MetaDemonTicket', peer, 'tickets_full')
                logger.info('本号门票已满，继续组队直到对方满票')
        if mode == TicketMode.AWAKE:
            soul = runner._config.switch_soul_config
            if soul.enable or soul.enable_switch_by_name:
                runner.goto_page(pages.page_shikigami_records)
                if soul.enable_switch_by_name:
                    runner.run_switch_soul_by_name(soul.group_name, soul.team_name)
                else:
                    runner.run_switch_soul(soul.switch_group_team)
            if config.meta_demon_ticket.general_config.awake_buff_enable:
                runner.goto_page(pages.page_main)
                runner.open_buff()
                runner.awake(is_open=True)
                buff_opened = True
                runner.close_buff()
            runner.limit_time = limit_time
            if role == ExplorationUserStatus.LEADER:
                runner.run_leader()
            elif role == ExplorationUserStatus.MEMBER:
                runner.run_member()
            else:
                runner.run_alone()
        else:
            runner.pre_process()
            runner.limit_time = limit_time
            runner.exec_exp_page()
    except TicketsFull:
        result = True
        if peer:
            logger.info('双方门票已满，结束协同刷取')
    except TicketPartnerFinished:
        logger.info('协同伙伴已结束门票刷取，退出组队')
    except TaskEnd:
        # 复用流程（EvoZone / Exploration）在限时或计数到达时会抛 TaskEnd。
        # 不在这里兜住的话它会一路冲出本任务的 run()，跳过 set_next_run ——
        # next_run 停在原值上，调度器会立刻再把本任务拉起来，形成死循环。
        logger.info('门票刷取流程自行结束（限时或计数到达）')
        result = local_full
    finally:
        runner._ticket_farming_complete = True
        if peer:
            runtime.notify_task_completion('MetaDemonTicket', peer, 'tickets_full' if result else 'ticket_finished')
        if peer or (not peer and role != ExplorationUserStatus.ALONE) or buff_opened:
            runner.goto_page(pages.page_main)
        if buff_opened:
            runner.open_buff()
            runner.awake(is_open=False)
            runner.close_buff()
    return result
