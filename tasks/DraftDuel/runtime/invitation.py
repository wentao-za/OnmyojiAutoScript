"""协同对弈：invitation。"""

import time
from module.logger import logger
from tasks.DraftDuel.config import DraftMatchMode
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.Component.GeneralInvite.general_invite import (
    FIRST_INVITE_RETRY_SECONDS, CONTINUE_INVITE_RETRY_SECONDS,
)
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.page import page_draft_duel
from tasks.GameUi.default_pages import page_reward, random_click
from tasks.DraftDuel.runtime.round_state import RUNTIME_CAPTAIN_CONFIRM_FRAMES


class DraftDuelInvitationMixin:
    def _dismiss_draft_lobby_overlay(self):
        """大厅背景可透过最终番胜或奖励页，优先点掉遮罩再处理大厅。"""
        if self.appear(self.I_FIGHT_STATISTIC):
            if self._read_statistic_result() is None:
                return True
            self._resolve_statistic_result()
        if not (any(self.appear(rule) for rule in
                    (self.I_FIGHT_RESULT, self.I_DRAFT_SHARE, self.I_FIGHT_STATISTIC))
                or page_reward.recognizer.evaluate(self)):
            return False
        logger.info('DraftDuel 检测到结算/奖励遮罩，先关闭再操作大厅')
        self._click_draft_settlement_continue()
        return True

    def _clear_draft_lobby_overlays(self):
        """有界清理连续遮罩；两张新截图均无结算附页后才允许大厅动作。"""
        deadline = time.monotonic() + CONTINUE_INVITE_RETRY_SECONDS + 15
        clear_frames = 0
        while time.monotonic() < deadline:
            if self._dismiss_draft_lobby_overlay() is True:
                clear_frames = 0
            else:
                clear_frames += 1
                if clear_frames >= 2:
                    return True
            self.device.stuck_record_clear()
            time.sleep(0.3)
            self.screenshot()
        logger.warning('DraftDuel 结算/奖励遮罩在限时内未清理干净')
        return False

    def _invite_teammate(self):
        """打开邀请最多三次；每次先清理附属结算页，再复核邀请面板或入队结果。"""
        for attempt in range(1, 4):
            self.screenshot()
            if not self._clear_draft_lobby_overlays():
                logger.warning(f'DraftDuel 第{attempt}/3次邀请被结算遮罩挡住')
                continue
            if (self.config.draft_duel.draft_duel_config.match_mode == DraftMatchMode.CAPTAIN
                    and self.appear(self.I_INVITE_CONTINUE)):
                self._click_draft_settlement_continue()
                return True
            if self.appear(self.I_INVITE_ENSURE):
                if self.invite_friends(self.config.draft_duel.invite_config, open_invite=False):
                    self._draft_last_invite_at = time.time()
                    self._draft_invite_retry_seconds = FIRST_INVITE_RETRY_SECONDS
                    return True
                continue
            if not self.appear(self.I_EMPTY_TEAMMATE) and self.is_in_room(False):
                return True
            if not self.appear_then_click(self.I_EMPTY_TEAMMATE, interval=1):
                logger.warning(f'DraftDuel 邀请入口未点中：第{attempt}/3次')
                continue
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.device.stuck_record_clear()
                self.screenshot()
                if self._dismiss_draft_lobby_overlay() is True:
                    time.sleep(0.3)
                    continue
                if self.appear(self.I_INVITE_ENSURE):
                    if self.invite_friends(self.config.draft_duel.invite_config, open_invite=False):
                        self._draft_last_invite_at = time.time()
                        self._draft_invite_retry_seconds = FIRST_INVITE_RETRY_SECONDS
                        return True
                    break
                if not self.appear(self.I_EMPTY_TEAMMATE) and self.is_in_room(False):
                    return True
                time.sleep(0.3)
            logger.warning(f'DraftDuel 邀请面板未确认，重试第{attempt}/3次')
        logger.warning('DraftDuel 邀请 3 次均失败')
        return False

    def is_in_room(self, is_screenshot=True):
        """为通用邀请提供基础图像判断，详细房间核验留给新流程。"""
        if is_screenshot:
            self.screenshot()
        if self.appear_accept() or self.appear(self.I_INVITE_ENSURE):
            return False
        if self.config.draft_duel.draft_duel_config.match_mode == DraftMatchMode.MEMBER:
            return self.appear(self.I_LOBBY_TEAMMATE_WAIT) or self._member_match_started()
        if not self._in_draft_lobby():
            return False
        # 空位「+」与「灰头像+齿轮」都表示座位上没有可用队友。
        return not self._teammate_absent()

    def _in_draft_lobby(self) -> bool:
        """大厅统一使用全局页面标志，业务按钮不作为页面判断。"""
        return self.appear(GameUiAssets.I_DRAFT_DUEL)

    def _goto_draft_lobby(self) -> bool:
        """复用全局路径：庭院 -> 町中 -> 协同对弈。"""
        self.goto_page(page_draft_duel)
        if not self._clear_draft_lobby_overlays():
            return False
        self._observe_duel_lobby()
        return self._in_draft_lobby()

    def _wait_for_teammate(self) -> bool:
        """发送邀请后，确认大厅空位消失；邀请面板遮挡不算入房。"""
        timeout = self.config.draft_duel.invite_config.wait_time_v.total_seconds()
        deadline = time.monotonic() + timeout
        while True:
            # 主动等人由邀请超时管理，避免被设备的无操作计时提前中断。
            self.device.stuck_record_clear()
            self.screenshot()
            if self._dismiss_draft_lobby_overlay() is True:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.3)
                continue
            if self.is_in_room(False):
                logger.info('DraftDuel empty_teammate 消失，队友已入队')
                return True
            if time.monotonic() >= deadline:
                logger.warning('DraftDuel 等待队友超时，不开始匹配')
                return False
            last = getattr(self, '_draft_last_invite_at', None)
            retry = getattr(self, '_draft_invite_retry_seconds', FIRST_INVITE_RETRY_SECONDS)
            if isinstance(last, (int, float)) and time.time() - last >= retry:
                # Continue button is gone: retry through the original friend invitation UI.
                self._draft_last_invite_at = time.time()
                self._invite_teammate()
            time.sleep(0.3)

    def _start_as_captain(self, timeout=10) -> bool:
        """以队长身份点「战」开始匹配；用于配置队员但运行时被推为队长的情况。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear_then_click(self.I_LOBBY_BATTLE, interval=1):
                logger.info('DraftDuel 已作为队长点击开始战斗')
                return True
            time.sleep(0.3)
        logger.warning(f'DraftDuel 队长开战按钮 {timeout}s 内未点中')
        return False

    def _enter_matchmaking(self) -> bool:
        """队员在当前页面等邀请；队长和单人仍先进入大厅。"""
        mode = self.config.draft_duel.draft_duel_config.match_mode
        solo_start = False
        if mode == DraftMatchMode.MEMBER:
            self.prepare_battle_cooperation('DraftDuel')
            return self._wait_member_start()
        if not self._goto_draft_lobby():
            return False
        if mode == DraftMatchMode.CAPTAIN:
            self.prepare_battle_cooperation('DraftDuel')
            self.screenshot()
            if not self._clear_draft_lobby_overlays():
                return False
            solo_start = self._teammate_quit_confirmed()
            if solo_start:
                if not self.config.draft_duel.draft_duel_config.solo_start_on_teammate_quit:
                    logger.warning('DraftDuel 队友已退出(灰头像+齿轮)且配置禁止单人开战, 结束任务')
                    return False
                logger.warning('DraftDuel 队友已退出(灰头像+齿轮), 按单人开始匹配')
            if not solo_start and self.appear(self.I_EMPTY_TEAMMATE):
                last = getattr(self, '_draft_last_invite_at', None)
                retry = getattr(self, '_draft_invite_retry_seconds', FIRST_INVITE_RETRY_SECONDS)
                recent = isinstance(last, (int, float)) and time.time() - last < retry
                if not recent and not self._invite_teammate():
                    return False
                if not self._wait_for_teammate():
                    self._finish_invite_wait_timeout()
                    return False
        if not self.wait_until_appear(self.I_LOBBY_BATTLE, wait_time=5):
            return False
        if not self._in_draft_lobby():
            return False
        # 等待按钮期间队友可能离开，点击前再检查一次；已确认退队的单人开战不再拦截。
        if mode == DraftMatchMode.CAPTAIN and not solo_start and not self.is_in_room(False):
            return False
        clicked = bool(self.appear_then_click(self.I_LOBBY_BATTLE, interval=1))
        if clicked:
            logger.info(f'DraftDuel {mode.value}：已点击 lobby_battle')
        return clicked

    def _member_match_started(self) -> bool:
        """以明确的选人/战斗标志确认开始，不把等待标志漏识别当开战。"""
        if self.appear(self.I_LOBBY_TEAMMATE_WAIT):
            return False
        return self._is_draft_selection_page() or any(self.appear(rule) for rule in (
            self.I_SELECT_1,
            self.I_SELECT_2,
            self.I_SELECT_3,
            self._battle_info_rule(),
            GeneralBattleAssets.I_PREPARE_HIGHLIGHT,
            GeneralBattleAssets.I_PREPARE_DARK,
        ))

    def _wait_member_start(self) -> bool:
        """在当前页面接收通用邀请，入队后最多等待队长开战 600 秒。"""
        deadline = time.monotonic() + self.MEMBER_START_TIMEOUT
        waiting_seen = False
        while True:
            self.device.stuck_record_clear()
            self.screenshot()
            if self._dismiss_draft_lobby_overlay() is True:
                if time.monotonic() >= deadline:
                    self._finish_member_wait_timeout()
                time.sleep(0.3)
                continue
            if self._member_match_started():
                logger.info('DraftDuel 队长已开始，队员已进入选人或战斗')
                return True
            if self._runtime_is_captain():
                # 队友退出或队长被系统转移后，本机看到的是「战」而不是「等待中」。
                # 连续两帧确认，避免「等待中」单帧漏识别被当成队长；确认后直接接管开战，
                # 否则只会空转到 MEMBER_START_TIMEOUT。
                self._draft_captain_frames = getattr(self, '_draft_captain_frames', 0) + 1
                if self._draft_captain_frames >= RUNTIME_CAPTAIN_CONFIRM_FRAMES:
                    logger.warning('DraftDuel 运行时身份变为队长(队友已退出或队长已转移), 转入队长开战')
                    return self._start_as_captain()
            else:
                self._draft_captain_frames = 0
            now = time.monotonic()
            if self.appear(self.I_LOBBY_TEAMMATE_WAIT) and not waiting_seen:
                waiting_seen = True
                deadline = now + self.MEMBER_START_TIMEOUT
                logger.info('DraftDuel 检测到 lobby_teammate_wait，等待队长至多 600s')
            if now >= deadline:
                self._finish_member_wait_timeout()
            if self.appear_accept():
                # 每次接受操作有界，截图轮询继续负责等人及处理总超时。
                self.check_then_accept(
                    timeout=min(5, deadline - now),
                    default_invite=self.config.draft_duel.invite_config.default_invite,
                )
            time.sleep(0.3)
