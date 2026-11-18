"""协同对弈：matchmaking。"""

import time
from module.logger import logger
from tasks.DraftDuel.config import DraftMatchMode
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets

# 「匹配中」面板与「战」按钮互斥：面板在场时按钮位换成「取消匹配」，面板消失则房间页可操作。
# 连续对局的匹配面板常常一闪而过，回到房间页后 active_since 会永久粘滞——既不点战
# （要求 active_since is None），也不判超时（判定写在面板命中分支内），只能等面板偶然
# 再出现才解锁（2026-11-18 oas1 实测第 3/4 小局分别空转 308s/312s 后靠一记取消爬出）。
MATCH_INTERRUPT_FRAMES = 10


class DraftDuelMatchmakingMixin:
    def _match_battle(self) -> bool | str:
        """交换名单后匹配；取消必须确认回到待匹配页面才能重开。"""
        leader = self.config.draft_duel.draft_duel_config.match_mode != DraftMatchMode.MEMBER
        limit = self.config.draft_duel.draft_duel_config.match_wait_seconds
        state = getattr(self, '_draft_match_state', None) or {}
        attempts = state.get('attempts', 0)
        active_wall = state.get('active_wall')
        click_wall = state.get('click_wall')
        current = time.monotonic()
        active_since = current - max(0, time.time() - active_wall) if active_wall is not None else None
        click_started = current - max(0, time.time() - click_wall) if click_wall is not None else None
        cancelling = state.get('cancelling', False)
        wait_miss = state.get('wait_miss', 0)
        last_battle_click = None
        progress_deadline = time.monotonic() + 600
        while True:
            self._draft_match_state = dict(attempts=attempts, active_wall=active_wall,
                                           click_wall=click_wall, cancelling=cancelling,
                                           wait_miss=wait_miss)
            self.device.stuck_record_clear()
            self.screenshot()
            self._save_draft_checkpoint('rank')
            now = time.monotonic()
            # 「转移队长」按钮说明本机是队长，不能当队友离队判据；
            # 队友离队只认座位形态（灰头像+右下角齿轮），见 round_state。
            if not leader and self._teammate_seat_quit(False):
                self._leave_draft_team('协同方已退出队伍')
                return False
            if self._dismiss_draft_lobby_overlay() is True:
                time.sleep(0.3)
                continue
            self._observe_duel_lobby()
            if self.appear(self.I_WAIT_FIGHT_LOBBY):
                self._draft_match_state = None
                self._save_draft_checkpoint('lobby')
                logger.info('DraftDuel 匹配期间回到组队大厅，转入下一大轮')
                return 'lobby'
            # 两个判据本帧各取一次，供复位判定与下面的分支链共用。
            matching = self.appear(self.I_FIGHT_WAIT)
            rank_page = self._is_draft_rank_page()
            # I_POST_DRAFT_RANK 在加载/过渡帧也会命中，所以「可操作房间页」还要按钮真的回到场上：
            # 匹配中面板占据按钮位，战斗加载期两者都不在，只有面板退去才会重新出现。
            room_ready = rank_page and (self.appear(self.I_LOBBY_BATTLE)
                                        or self.appear(self.I_LOBBY_TEAMMATE_WAIT))
            if matching:
                wait_miss = 0
            elif active_since is not None and not cancelling and room_ready:
                wait_miss += 1
            else:
                wait_miss = 0
            # 已进入匹配却回到可操作房间页：面板连续缺席满 MATCH_INTERRUPT_FRAMES 帧，
            # 或等待已越过上限，说明这次匹配被打断（对手取消、界面回退等）。清除计时后
            # 由下面的房间页分支重新点战；此处不点取消——该界面已无「取消匹配」入口。
            if (room_ready and active_since is not None and not cancelling and not matching
                    and (wait_miss >= MATCH_INTERRUPT_FRAMES or now - active_since >= limit)):
                logger.warning(f'DraftDuel 匹配中面板消失且回到房间页（连续缺席{wait_miss}帧），'
                               f'判定本次匹配被打断，重置计时后重新点战')
                active_since = active_wall = None
                click_started = click_wall = None
                last_battle_click = None
                wait_miss = 0
                progress_deadline = now + 600
                self._draft_match_state = dict(attempts=attempts, cancelling=False, wait_miss=0)
                time.sleep(0.3)
                continue
            # 取消返回页时“匹配中”标志可能仍命中，开始按钮优先证明已回到待匹配页。
            cancel_returned = leader and cancelling and self.appear(self.I_LOBBY_BATTLE)
            if cancel_returned:
                cancelling = False
                active_since = active_wall = None
                click_started = click_wall = None
                last_battle_click = None
                progress_deadline = now + 600
                logger.info(f'DraftDuel 第{attempts}/3次取消匹配已确认回到待匹配页')
            if self.appear(self.I_EXIT_CANCEL):
                self.appear_then_click(self.I_EXIT_CANCEL, interval=1)
            elif not cancel_returned and matching:
                self._begin_small_game()
                if active_since is None:
                    active_since = now
                    active_wall = time.time()
                    click_started = None
                    click_wall = None
                    last_battle_click = None
                    if leader:
                        attempts += 1
                        logger.info(f'DraftDuel 第{attempts}/3次匹配，超时={limit}s')
                    progress_deadline = now + limit + 600
                if leader and now - active_since >= limit:
                    cancelling = True
                    self.appear_then_click(self.I_FIGHT_CANCEL_WAIT_LEADER, interval=1)
            elif cancel_returned or rank_page:
                self._observe_victory_count()
                require_auto = getattr(self, 'draft_finished_small_games', 0) > 0
                if require_auto:
                    # 每帧重新确认，点击后必须在后续截图看到 auto_right。
                    self._draft_auto_ready_after = (
                        self.draft_finished_small_games if self.appear(self.I_AUTO_RIGHT) else None)
                    if self._draft_auto_ready_after is None:
                        self.appear_then_click(self.I_AUTO_DOWN, interval=1)
                    else:
                        self._release_deployment_templates()
                roster_ok = self._sync_draft_state(require_auto=require_auto)
                auto_ok = (not require_auto) or (self._draft_auto_ready_after is not None)
                if not roster_ok or not auto_ok:
                    if roster_ok:
                        self._draft_sync_fail_since = None
                    else:
                        # 名单失效兜底（方案 E 一期）：连续失败 5s 才动用，避免
                        # 「协同方还没发布」这类瞬态白烧掉每大轮唯一一次机会。
                        if getattr(self, '_draft_sync_fail_since', None) is None:
                            self._draft_sync_fail_since = now
                        if (now - self._draft_sync_fail_since >= 5
                                and getattr(self, '_draft_roster_rescue_round', -1)
                                != getattr(self, 'draft_big_round', 0)):
                            self._draft_roster_rescue_round = getattr(self, 'draft_big_round', 0)
                            logger.info('DraftDuel 名单同步已连续失败 5s，触发房间页名单恢复')
                            self._rescue_roster_from_room()
                            # 本轮机会已用完：清空计时，下一大轮重新累计 5s。
                            self._draft_sync_fail_since = None
                    if now >= progress_deadline:
                        logger.warning('DraftDuel 等待协同方名单超时')
                        return False
                    time.sleep(0.3)
                    continue
                self._draft_sync_fail_since = None
                if (not getattr(self, 'draft_planned_lineup', ())
                        and getattr(self, 'draft_small_game', 0) == 0):
                    try:
                        if self._selection_cooperation_enabled():
                            if not self._prepare_captain_lineup():
                                self._log_sync_wait('等待队长下发首次上阵名单')
                                time.sleep(0.3)
                                continue
                        else:
                            self.draft_planned_lineup = tuple(self._deployment_plan())
                    except ValueError as exc:
                        logger.warning(f'DraftDuel 战前名单不可用：{exc}')
                        self._leave_draft_team('无法规划上阵名单', success=False)
                        return False
                    logger.info(f'DraftDuel 战前名单：{self.draft_planned_lineup}')
                    self._save_draft_checkpoint('rank')
                if leader and active_since is None and not cancelling:
                    if attempts >= 3:
                        self._finish_match_timeout()
                    if click_started is None:
                        click_started = now
                        click_wall = time.time()
                    if now - click_started >= 30:
                        logger.warning('DraftDuel 战斗按钮重试 30s 仍未进入匹配')
                        return False
                    if last_battle_click is None or now - last_battle_click >= 3:
                        self.appear_then_click(self.I_LOBBY_BATTLE, interval=3)
                        last_battle_click = now
            elif self.appear(self.I_POST_DRAFT_FIGHT):
                self._begin_small_game()
                self._draft_matching_bout_active = False
                self._draft_match_state = None
                return True
            elif any(self.appear(rule) for rule in (
                    self._battle_info_rule(),
                    GeneralBattleAssets.I_PREPARE_HIGHLIGHT,
                    GeneralBattleAssets.I_PREPARE_DARK)):
                self._draft_match_state = None
                return True
            if click_started is not None and active_since is None and now - click_started >= 30:
                logger.warning('DraftDuel 战斗按钮重试 30s 仍未进入匹配')
                return False
            if now >= progress_deadline:
                logger.warning('DraftDuel 匹配页 600s 无进展')
                return False
            time.sleep(0.3)
