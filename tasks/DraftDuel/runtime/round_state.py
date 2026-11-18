"""协同对弈：round_state。"""

import time
from module.logger import logger
from tasks.GameUi.assets import GameUiAssets

# 队友退队判据：座位保留、头像变灰、右下角出现齿轮（2026-11-18 实机四帧标定：
# 命中帧 1.000，正常帧 ≤0.309，另一座位 ≤0.136；亮度/模糊/JPEG 扰动后仍 ≥0.968）
TEAMMATE_QUIT_THRESHOLD = 0.80
TEAMMATE_QUIT_CONFIRM_FRAMES = 2
TEAMMATE_QUIT_FRAME_INTERVAL = 0.3
# 运行时身份纠偏：连续两帧都只看到「战」才认队长，防止「等待中」单帧漏识别被当成队长
RUNTIME_CAPTAIN_CONFIRM_FRAMES = 2


class DraftDuelRoundStateMixin:
    def _is_draft_selection_page(self):
        """顶部阶段文字被遮挡时，用至少两个独立的选择按钮确认选人页。"""
        if self.appear(self.I_POST_DRAFT_SELECT):
            return True
        return sum(bool(self.appear(rule)) for rule in (
            self.I_SELECT_1, self.I_SELECT_2, self.I_SELECT_3)) >= 2

    def _is_draft_rank_page(self):
        """等待匹配备用识别：自动上阵状态与队长/队员开始按钮同时可见。"""
        if self.appear(self.I_POST_DRAFT_RANK):
            return True
        if not any(self.appear(rule) for rule in (self.I_AUTO_DOWN, self.I_AUTO_RIGHT)):
            return False
        if self.appear(self.I_POST_DRAFT_FIGHT) or self.appear(self.I_WAIT_FIGHT_LOBBY):
            return False
        return any(self.appear(rule) for rule in (
            self.I_LOBBY_BATTLE, self.I_LOBBY_TEAMMATE_WAIT))

    def _teammate_seat_quit(self, screenshot=True):
        """队友座位是否为「已退队」形态（头像变灰 + 右下角齿轮）。

        一条 ROI 同时覆盖两个座位：自己在线时自己那份不会显示齿轮，
        所以命中任一座位即可判定队友已退，不必先判断哪边是自己。
        未开战即退队走「+」空位形态，由 I_EMPTY_TEAMMATE 判定，见 _teammate_absent。
        """
        if screenshot:
            self.screenshot()
        return bool(self.appear(self.I_TEAMMATE_QUIT, threshold=TEAMMATE_QUIT_THRESHOLD))

    def _teammate_quit_confirmed(self):
        """连续 TEAMMATE_QUIT_CONFIRM_FRAMES 帧命中齿轮才认定队友已退。

        组队大厅/番战页都适用；结果页、奖励页遮罩会盖住座位，遮罩帧跳过后补采，
        但不计入命中次数。任一帧未命中即判否；遮罩久不消失时按上限退出，不死循环。
        """
        hits = 0
        attempts = 0
        limit = TEAMMATE_QUIT_CONFIRM_FRAMES + 3
        while hits < TEAMMATE_QUIT_CONFIRM_FRAMES and attempts < limit:
            attempts += 1
            self.device.stuck_record_clear()
            self.screenshot()
            if self._dismiss_draft_lobby_overlay() is True:
                continue
            if not self._teammate_seat_quit(False):
                return False
            hits += 1
            if hits < TEAMMATE_QUIT_CONFIRM_FRAMES:
                time.sleep(TEAMMATE_QUIT_FRAME_INTERVAL)
        return hits >= TEAMMATE_QUIT_CONFIRM_FRAMES

    def _teammate_absent(self):
        """座位上没有可用队友：未开战的「+」空位，或已退队的灰头像+齿轮。"""
        return bool(self.appear(self.I_EMPTY_TEAMMATE)) or self._teammate_seat_quit(False)

    def _runtime_is_captain(self, screenshot=False):
        """以界面按钮判定运行时身份：队长看到「战」，队员看到「等待中」，同一 ROI 互斥。

        用作配置身份的运行时纠偏：队长被系统转给本机后，配置仍写着 MEMBER。
        """
        if screenshot:
            self.screenshot()
        battle = self.appear(self.I_LOBBY_BATTLE)
        wait = self.appear(self.I_LOBBY_TEAMMATE_WAIT)
        return bool(battle and not wait)

    def _initialize_duel_state(self):
        """初始化内存容器；初始化本身不代表观察到新一轮。"""
        self.draft_heads = ()
        self.draft_named_heads = ()
        self.draft_hand_templates = ()
        self._draft_hand_capture_clock = None
        self.draft_planned_lineup = ()
        self.draft_selected = []
        self._draft_transcript = {}
        self._draft_detail_capture_rounds = set()
        self._draft_panel_snapshot = None
        self._draft_theoretical_lineup = ()
        self._draft_deployment_overrides = {}
        self._draft_peer_transcript = []
        self.draft_fielded = ()
        self.draft_deployment_rounds = []
        self.draft_peer_picks = ()
        self.draft_peer_rounds = ()
        self._draft_session = ''
        self._draft_published_state = None
        self._draft_offer = None
        self._draft_pick_decision = None
        self._draft_lineup_plan = None
        self._draft_peer_offer = None
        self._draft_peer_decision = None
        self._draft_peer_lineup_plan = None
        self._draft_exchange_since = time.time()
        self._draft_auto_ready_after = None
        self.draft_finished_small_games = 0
        self.draft_small_game = 0
        self._draft_matching_bout_active = False
        self._draft_round_had_battle = False
        self._draft_battle_seen = False
        self._draft_clock_candidate = None
        self._draft_deploy_clock_wall = None
        self._draft_end_recorded = False
        self._draft_deployment_state = None
        self._draft_match_state = None
        self._draft_selection_pending = None
        self._draft_field_weights = None
        self.draft_unresolved_slots = []
        self._draft_small_game_result = None
        self._initialize_round_statistics()

    def _observe_duel_lobby(self):
        """只有重新进入组队大厅，才开启新大轮；连续大厅截图只计一次。"""
        in_lobby = bool(self.appear(self.I_WAIT_FIGHT_LOBBY))
        if in_lobby and not getattr(self, '_draft_wait_lobby_seen', False):
            self._draft_wait_lobby_seen = True
            if getattr(self, '_draft_round_had_battle', False):
                self._finalize_round_stats(completed=True, reason='返回wait_fight_lobby')
                self.draft_completed_rounds = getattr(self, 'draft_completed_rounds', 0) + 1
                self._draft_round_had_battle = False
                target = self.config.draft_duel.draft_duel_config.round_count
                logger.info(f'DraftDuel 已完成大轮：{self.draft_completed_rounds}/{target}')
                if self.draft_completed_rounds >= target:
                    self._leave_draft_team(f'已完成{self.draft_completed_rounds}大轮协斗')
                    return
            self._initialize_duel_state()
            self.draft_big_round = getattr(self, 'draft_big_round', 0) + 1
            logger.info(f'DraftDuel 新大轮：{self.draft_big_round}')
        if in_lobby:
            self._draft_wait_lobby_seen = True
        elif getattr(self, '_draft_wait_lobby_seen', False) and (
                self._is_draft_selection_page() or self._is_draft_rank_page()
                or any(self.appear(rule) for rule in (
                    self.I_POST_DRAFT_FIGHT, self.I_FIGHT_WAIT, GameUiAssets.I_CHECK_TOWN))):
            # 邀请面板或单帧漏识别不算离开大厅，避免重新看到大厅时误清本轮。
            self._draft_wait_lobby_seen = False

    def _begin_small_game(self):
        """post_draft_rank 发起的小局；取消匹配重试不增加小局次数。"""
        if not getattr(self, '_draft_matching_bout_active', False):
            self._draft_battle_seen = False
            self._draft_end_recorded = False
            self._draft_small_game_result = None
            self.draft_small_game = getattr(self, 'draft_small_game', 0) + 1
            self._draft_matching_bout_active = True
            # 首局确定的阵容会被后续自动上阵复用，保留实际名单用于胜负加权。
            if self.draft_small_game == 1:
                self.draft_fielded = ()
                self.draft_deployment_rounds = []
                self.draft_peer_rounds = ()
                self.draft_unresolved_slots = []
            self._draft_deployment_state = None
            logger.info(f'DraftDuel 本大轮第{self.draft_small_game}小局')
