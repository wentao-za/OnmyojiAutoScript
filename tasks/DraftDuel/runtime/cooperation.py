"""协同对弈：cooperation。"""

import time
import uuid
import math
from module.logger import logger
from tasks.DraftDuel.config import DraftMatchMode
from tasks.DraftDuel.strategy import normalize_name
from tasks.DraftDuel.strategy import battle_stats


class DraftDuelCooperationMixin:
    def _publish_draft_abort(self, reason):
        settings = getattr(getattr(self, 'config', None), 'draft_duel', None)
        runtime = getattr(getattr(self, 'config', None), 'cooperation_runtime', None)
        session = getattr(self, '_draft_session', '')
        if not settings or not runtime or not session:
            return
        peer = settings.invite_config.cooperation_script.strip()
        if not peer or settings.draft_duel_config.match_mode == DraftMatchMode.SOLO:
            return
        try:
            runtime.publish_battle_state('DraftDuel', peer, session, {
                'small_game': getattr(self, 'draft_small_game', 0),
                'picks': list(getattr(self, 'draft_selected', ())),
                'rounds': list(getattr(self, 'draft_deployment_rounds', ())),
                'abort_reason': reason,
            })
        except Exception as exc:
            logger.warning(f'DraftDuel 无法发布轮次中止：{exc}')

    def _sync_draft_state(self, require_small_game=False, require_auto=False, allow_partial=False) -> bool:
        """复用通用协同数据库，交换同一局备选和每轮已确认上阵信息。"""
        peer = self.config.draft_duel.invite_config.cooperation_script.strip()
        if not peer or self.config.draft_duel.draft_duel_config.match_mode == DraftMatchMode.SOLO:
            # 显式留空即关闭名单协同，忽略自动接入或上次运行残留的 peer。
            self.draft_peer_picks = ()
            self.draft_peer_rounds = ()
            self._draft_sync_wait_reason = None
            return True
        runtime = getattr(self.config, 'cooperation_runtime', None)
        if runtime is None:
            self._log_sync_wait(f'已配置协同脚本 {peer}，等待协同运行时就绪')
            return False
        if not hasattr(self, '_draft_exchange_since'):
            self._draft_exchange_since = time.time()
        mode = self.config.draft_duel.draft_duel_config.match_mode
        remote = runtime.peer_battle_state('DraftDuel', peer)
        if not getattr(self, '_draft_session', ''):
            if mode == DraftMatchMode.CAPTAIN:
                self._draft_session = uuid.uuid4().hex
            elif remote and remote['updated'] >= self._draft_exchange_since:
                self._draft_session = remote['session']
            else:
                self._log_sync_wait(f'等待队长 {peer} 发布本大轮名单')
                return False
        payload = {'small_game': getattr(self, 'draft_small_game', 0),
                   'picks': list(self.draft_selected),
                   'rounds': list(getattr(self, 'draft_deployment_rounds', ())),
                   'field_weights': getattr(self, '_draft_field_weights', None),
                   'auto_ready_after': getattr(self, '_draft_auto_ready_after', None),
                   'offer': getattr(self, '_draft_offer', None),
                   'pick_decision': getattr(self, '_draft_pick_decision', None),
                   'lineup_plan': getattr(self, '_draft_lineup_plan', None),
                   'draft_rounds': list(getattr(self, '_draft_transcript', {}).values())}
        if payload != getattr(self, '_draft_published_state', None):
            runtime.publish_battle_state('DraftDuel', peer, self._draft_session, payload)
            self._draft_published_state = payload
        if not remote or remote['session'] != self._draft_session:
            self._log_sync_wait(f'等待协同脚本 {peer} 交换同一大轮的五名式神；尚未收到本轮名单')
            return False
        data = remote['payload']
        if data.get('abort_reason'):
            self._leave_draft_team(f"协同方已结束本轮：{data['abort_reason']}", success=False)
            return False
        self._draft_peer_offer = data.get('offer')
        self._draft_peer_decision = data.get('pick_decision')
        self._draft_peer_lineup_plan = data.get('lineup_plan')
        self._draft_peer_transcript = data.get('draft_rounds', [])
        picks = data.get('picks', [])
        rounds = data.get('rounds', [])
        if ((not allow_partial and len(picks) != 5) or len(picks) > 5 or
                len(set(picks)) != len(picks) or any(normalize_name(n) != n for n in picks)):
            self._log_sync_wait(f'协同脚本 {peer} 的五名式神名单不完整或包含未知名称')
            return False
        if (len(rounds) > 3 or any(r.get('name') not in picks or
                                 r.get('round') not in range(1, 4) or r.get('slot') not in range(1, 7)
                                 for r in rounds) or
                [r['round'] for r in rounds] != sorted(set(r['round'] for r in rounds))):
            return False
        self.draft_peer_picks = tuple(picks)
        same_game = data.get('small_game', 0) == getattr(self, 'draft_small_game', 0)
        self.draft_peer_rounds = tuple(rounds) if same_game else ()
        if same_game and len(self.draft_selected) == len(picks) == 5:
            if mode == DraftMatchMode.CAPTAIN:
                if getattr(self, '_draft_field_weights', None) is None:
                    table = battle_stats._tally('fielded')
                    self._draft_field_weights = {
                        name: battle_stats.field_bonus(name, table)
                        for name in set((*self.draft_selected, *picks))}
                    payload['field_weights'] = self._draft_field_weights
                    runtime.publish_battle_state('DraftDuel', peer, self._draft_session, payload)
                    self._draft_published_state = payload
                    self._save_draft_checkpoint()
            else:
                weights = data.get('field_weights')
                expected = set((*self.draft_selected, *picks))
                if not isinstance(weights, dict) or set(weights) != expected or any(
                        type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 8
                        for v in weights.values()):
                    self._log_sync_wait('等待队长发布本大轮的固定实战评分')
                    return False
                else:
                    self._draft_field_weights = dict(weights)
                    self._save_draft_checkpoint()
        if require_auto:
            finished = getattr(self, 'draft_finished_small_games', 0)
            if (finished < 1 or getattr(self, '_draft_auto_ready_after', None) != finished
                    or type(data.get('auto_ready_after')) is not int
                    or data['auto_ready_after'] != finished):
                self._log_sync_wait(f'等待双方确认 auto_right（已完成{finished}小局）')
                return False
        if require_small_game and not same_game:
            self._log_sync_wait(f'等待协同脚本 {peer} 进入第{getattr(self, "draft_small_game", 0)}小局')
        else:
            if getattr(self, '_draft_sync_wait_reason', None):
                logger.info(f'DraftDuel 协同名单已就绪：{peer}')
            self._draft_sync_wait_reason = None
        return same_game if require_small_game else True

    def _captain_selection(self, number, names, template_keys=None):
        """队员上报候选；决策绑定双方已选前缀和候选，拒绝旧轮指令。"""
        from tasks.DraftDuel.strategy import Pick
        from tasks.DraftDuel.strategy.runtime_policy import choose_pick
        self._draft_offer = dict(number=number, names=list(names), picks=list(self.draft_selected))
        if template_keys:
            self._draft_offer['template_keys'] = {name: key for name, key in template_keys.items() if name and key}
        self._sync_draft_state(allow_partial=True)
        captain = self.config.draft_duel.draft_duel_config.match_mode == DraftMatchMode.CAPTAIN
        if captain:
            other = getattr(self, '_draft_peer_offer', None)
            if (not isinstance(other, dict) or other.get('number') != number or
                    len(other.get('picks', ())) != number - 1 or
                    other.get('picks') != list(getattr(self, 'draft_peer_picks', ())) or
                    not isinstance(other.get('names'), list) or len(other['names']) != 3 or
                    any(n and normalize_name(n) != n for n in other['names'])):
                return None
            previous = getattr(self, '_draft_pick_decision', None)
            if (previous and previous.get('captain_offer') == self._draft_offer and
                    previous.get('member_offer') == other):
                decision = previous
            else:
                own_keys = self._draft_offer.get('template_keys')
                peer_keys = other.get('template_keys')
                own_pick = choose_pick(self, [n for n in names if n], team=self.draft_selected,
                                      **({'template_keys': own_keys} if own_keys else {}))
                peer_pick = choose_pick(self, [n for n in other['names'] if n], team=other['picks'],
                                       **({'template_keys': peer_keys} if isinstance(peer_keys, dict) else {}))
                a, b = own_pick.name, peer_pick.name
                score = own_pick.score + peer_pick.score
                decision = dict(captain_offer=self._draft_offer, member_offer=other,
                                captain_pick=a, member_pick=b, score=score)
                self._draft_pick_decision = decision
                self._save_draft_checkpoint('select')
            self._sync_draft_state(allow_partial=True)
            name = decision['captain_pick']
        else:
            decision = getattr(self, '_draft_peer_decision', None)
            if not isinstance(decision, dict) or decision.get('member_offer') != self._draft_offer:
                return None
            name = decision.get('member_pick')
        if name not in names:
            return None
        return Pick(name, decision['score'], ('队长使用固定策略分别选择双方候选',), ())

    def _prepare_captain_lineup(self):
        """首次匹配由队长下发双方三人名单；队员不调用阵容优化器。"""
        from tasks.DraftDuel.strategy.runtime_policy import plan_duel_lineup
        captain = self.config.draft_duel.draft_duel_config.match_mode == DraftMatchMode.CAPTAIN
        if captain:
            plan = getattr(self, '_draft_lineup_plan', None)
            if not plan:
                own, peer = plan_duel_lineup(self, self.draft_selected, self.draft_peer_picks,
                                             field_weights=self._draft_field_weights)
                plan = self._draft_lineup_plan = dict(captain=list(own), member=list(peer))
            self.draft_planned_lineup = tuple(plan['captain'])
            self._sync_draft_state()
        else:
            plan = getattr(self, '_draft_peer_lineup_plan', None)
            if not isinstance(plan, dict):
                return False
            own, peer = plan.get('member', []), plan.get('captain', [])
            if (len(own) != 3 or len(peer) != 3 or len(set(own)) != 3 or len(set(peer)) != 3 or
                    not set(own).issubset(self.draft_selected) or
                    not set(peer).issubset(self.draft_peer_picks)):
                return False
            self._draft_lineup_plan = plan
            self.draft_planned_lineup = tuple(own)
        self._save_draft_checkpoint('rank')
        return True

    def _rescue_roster_from_room(self):
        """名单同步失败后，从房间页「阵容详情」弹窗重识别最终五人名单（方案 E 一期）。

        详情窗五列与第 1~5 名选人顺序一致（2026-11-18 oas3/oas4 双阵容实测），
        名单以弹窗 OCR 为准；`_draft_transcript` 仅在对应列 OCR 失败时回填
        （实测存在「日志确认选人、游戏实际未生效」的偏差，记录不可全信）。
        识别结果不完整或含未知名称时保守放弃，维持现有等待/超时行为。
        """
        deadline = time.monotonic() + 30
        own = list(getattr(self, 'draft_selected', []))
        logger.info('DraftDuel 名单同步失败，开始从阵容详情弹窗重识别')
        self.device.stuck_record_clear()
        self.screenshot()
        if not self._is_draft_rank_page():
            logger.warning('DraftDuel 中止名单恢复：当前不在协同对弈房间页')
            return
        if not self.appear(self.I_ROSTER_DETAIL):
            # 只在弹窗关闭时点顶部「我的阵容」牌；弹窗已开时盲点会落进弹窗内部，
            # 可能误开式神详情，导致后续识别与点击全部错位。
            self.click(self.C_ROSTER_BAR)
            if not self._wait_roster_view(self.I_ROSTER_DETAIL,
                                          time.monotonic() + 4, leave_check=True):
                if not self._is_draft_rank_page():
                    logger.warning('DraftDuel 中止名单恢复：首次点击后房间页消失，不再重试点击')
                    return
                logger.warning('DraftDuel 首次点击未打开阵容详情，重试一次')
                self.click(self.C_ROSTER_BAR)
        if not self._wait_roster_view(self.I_ROSTER_DETAIL, deadline, leave_check=True):
            logger.warning('DraftDuel 中止名单恢复：阵容详情弹窗未打开')
            return
        if not self._ensure_roster_mine(deadline):
            self._close_roster_detail(deadline)
            return
        if not self._settle_roster_view(self.I_ROSTER_MINE_ON):
            logger.warning('DraftDuel 中止名单恢复：「我的」视图切换后未稳定')
            self._close_roster_detail(deadline)
            return
        names = self._ocr_roster_names()
        peer_names = ()
        if self.appear_then_click(self.I_ROSTER_PEER_OFF, interval=1):
            if self._wait_roster_view(self.I_ROSTER_PEER_ON, deadline) \
                    and self._settle_roster_view(self.I_ROSTER_PEER_ON):
                peer_names = self._ocr_roster_names()
            else:
                logger.warning('DraftDuel 队友视图切换未确认')
        if not self._close_roster_detail(deadline):
            logger.warning('DraftDuel 中止名单恢复：弹窗未能关闭，保持原名单')
            return
        overlap = [name for name in names if name and name in peer_names]
        if overlap:
            logger.warning(f'DraftDuel 中止名单恢复：本方与队友名单出现交集 {overlap}，'
                           f'疑似读到对方视图，保持原名单')
            return
        final = self._canonical_rescued_roster(names)
        if final is None:
            return
        if final == own:
            logger.info(f'DraftDuel 名单与本机记录一致 {final}，等待协同方自恢复')
        else:
            logger.warning(f'DraftDuel 以阵容详情为准更新名单 {own} -> {final}')
            self.draft_selected = tuple(final)
        if peer_names:
            shown = '、'.join(peer_names)
            logger.info(f'DraftDuel 队友阵容 {shown}')

    def _ocr_roster_names(self):
        """按列读取阵容详情弹窗的五个式神名。"""
        names = []
        for rule in (self.O_ROSTER_NAME_1, self.O_ROSTER_NAME_2, self.O_ROSTER_NAME_3,
                     self.O_ROSTER_NAME_4, self.O_ROSTER_NAME_5):
            names.append(str(rule.ocr(self.device.image) or '').strip())
        return names

    def _canonical_rescued_roster(self, names):
        """OCR 五列 → 归一化名单；失败列用 transcript 同轮记录回填，仍失败则放弃。"""
        transcript = getattr(self, '_draft_transcript', {})
        final = []
        for index, raw in enumerate(names):
            name = normalize_name(raw) if raw else None
            if name is None:
                record = transcript.get(index + 1)
                fallback = record.get('pick') if isinstance(record, dict) else None
                if fallback and normalize_name(fallback) == fallback:
                    logger.warning(f'DraftDuel 第{index + 1}列 OCR 不可读（{raw!r}），'
                                   f'从选人记录回填：{fallback}')
                    name = fallback
            if name is None:
                logger.warning(f'DraftDuel 中止名单恢复：第{index + 1}列无法归一化'
                               f'（OCR={raw!r}），保持原名单')
                return None
            final.append(name)
        if len(set(final)) != 5:
            logger.warning(f'DraftDuel 中止名单恢复：识别名单有重复 {final}，保持原名单')
            return None
        return final

    def _wait_roster_view(self, rule, deadline, leave_check=False):
        """等待某素材出现；leave_check 时连续多帧既无素材又无房间页则判离场。"""
        absent = 0
        while time.monotonic() < deadline:
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(rule):
                return True
            if leave_check:
                # 弹窗打开会遮挡 I_POST_DRAFT_RANK，连续缺席才判离场，防动画帧误判。
                absent = 0 if self._is_draft_rank_page() else absent + 1
                if absent >= 4:
                    logger.warning('DraftDuel 中止名单恢复：房间页已消失'
                                   '（可能已进入匹配，或误开了其他界面）')
                    return False
            time.sleep(0.3)
        return False

    def _ensure_roster_mine(self, deadline):
        """弹窗可能停留在队友视图，确保切回「我的」后再采集本机名单。"""
        if self.appear(self.I_ROSTER_MINE_ON):
            return True
        while time.monotonic() < deadline:
            self.device.stuck_record_clear()
            self.screenshot()
            if self.appear(self.I_ROSTER_MINE_ON):
                return True
            if self.appear(self.I_ROSTER_PEER_ON):
                self.appear_then_click(self.I_ROSTER_MINE_OFF, interval=1)
            time.sleep(0.3)
        logger.warning('DraftDuel 中止名单恢复：未能定位「我的」阵容视图')
        return False

    def _close_roster_detail(self, deadline):
        """关闭阵容详情弹窗并确认回到房间页。

        弹窗的关闭 X 与房间页「排名」按钮几乎同坐标（实测关闭落点 (1211,125)
        正压在「排名」菱牌上），所以点击必须限量：最多两次，第二次要求
        「距上次点击 ≥1.2s 且弹窗连续两帧仍可见」，避免关窗动画期间连点
        落到下层「排名」上，把房间页换成排名面板（外层会空转到 600s 超时）。
        """
        end = max(deadline, time.monotonic()) + 10
        clicks = 0
        last_click = 0.0
        streak = 0
        while time.monotonic() < end:
            self.device.stuck_record_clear()
            self.screenshot()
            if self._is_draft_rank_page():
                return True
            if not self.appear(self.I_ROSTER_DETAIL):
                streak = 0
                time.sleep(0.3)
                continue
            streak += 1
            retry_ready = (clicks == 1 and streak >= 2
                           and time.monotonic() - last_click >= 1.2)
            if clicks == 0 or retry_ready:
                self.appear_then_click(self.I_ROSTER_CLOSE)
                clicks += 1
                last_click = time.monotonic()
                streak = 0
                time.sleep(0.6)
                continue
            time.sleep(0.3)
        if self.appear(self.I_ROSTER_DETAIL):
            logger.warning('DraftDuel 关闭按钮无效，尝试点击弹窗外区域关闭')
            self.click(self.C_SAFE_AREA)
            time.sleep(0.6)
            self.device.stuck_record_clear()
            self.screenshot()
            if self._is_draft_rank_page():
                return True
            if self.appear(self.I_ROSTER_DETAIL):
                logger.warning('DraftDuel 关闭弹窗失败，放弃本轮恢复（避免误点「排名」）')
                return False
            logger.warning('DraftDuel 弹窗已关闭但未确认回到房间页，按已关闭处理')
            return True
        # 弹窗已不在而房间页判据未命中：多为动画/判据滞后，按已关闭处理。
        return True

    def _settle_roster_view(self, rule, settle=0.4):
        """视图切换后等待重绘，并用新截图二次确认按钮态。

        按钮高亮与名单行重绘不同步，切完立即 OCR 可能把上一视图的名单
        读成当前视图（把队友阵容写成自己的）。
        """
        time.sleep(settle)
        self.device.stuck_record_clear()
        self.screenshot()
        return bool(self.appear(rule))

    def _log_sync_wait(self, reason):
        if getattr(self, '_draft_sync_wait_reason', None) != reason:
            logger.info(f'DraftDuel 协同等待：{reason}')
            self._draft_sync_wait_reason = reason
