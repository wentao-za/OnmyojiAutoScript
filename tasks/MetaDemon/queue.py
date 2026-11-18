# This Python file uses the following encoding: utf-8
"""超鬼王召唤队列域：开窗、判空、反应式补票、票尽驻留。

职责边界
--------
- **有没有票**：打开「裂变之眼召唤队列」弹窗（`ensure_ticket`），把「可直接挑战 /
  有票可选 / 本轮不能继续」三态交给 `boss_battle.summon_and_fire`。
- **没票了怎么办**：读弹窗里的掉落进度 (x/50)，未满就把 `refill_task` 指定的任务
  拉起来补票（`try_refill_ticket` → `config.task_call`）；补票用尽则按现场三态收尾，
  必要时转入驻留等待。
- **不负责**：选卡召唤（`boss_battle.summon_and_fire`）与主动分享（`assistance`）。

**刷票不在这里**。把票真正刷出来是独立任务「鬼王门票」（`tasks/MetaDemonTicket`）
的职责，本模块只做「战斗中没票了」这一步的判定与交棒 —— 这两件事 2026-11-10 之前
各有一份拷贝（`tasks/MetaDemon/ticket_farming.py` 与 `tasks/MetaDemonTicket/ticket_farming.py`），
已收敛为唯一入口 `MetaDemonTicket`。
"""
import re
import time
from enum import Enum

from module.base.timer import Timer
from module.config.utils import convert_to_underscore
from module.logger import logger

from tasks.MetaDemon.md_ui import MdUiMixin


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


class TicketQueueMixin(MdUiMixin):
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
