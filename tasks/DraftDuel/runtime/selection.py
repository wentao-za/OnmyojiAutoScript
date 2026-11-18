"""协同对弈：selection。"""

import re
import time
import numpy as np
from module.logger import logger
from tasks.DraftDuel.strategy import normalize_name
from tasks.DraftDuel.config import DraftMatchMode


class DraftDuelSelectionMixin:
    DETAIL_OPEN_POLL_INTERVAL = 0.3
    DETAIL_OPEN_TIMEOUT = 1.5
    DETAIL_CLICK_LIMIT = 3
    DETAIL_PANEL_SIDES = ('MEDIUM', 'RIGHT', 'LEFT')
    DETAIL_PANEL_RANGE_INDEXES = (1, 2)

    def _detail_panel_open(self) -> bool:
        """属性详情面板正判据：任一面板能读到「生命」「防御」两行数值。

        「属性详情」放大镜图标消失既可能是面板已展开（图标被面板自身盖住），也可能是
        被技能弹窗盖住，所以不能拿图标当判据。改用面板内容本身：面板展开后每张卡都有
        8 行数值，未展开时那里是雷达图。弹窗只盖住部分面板，因此任一面板命中即可。
        """
        image = getattr(self.device, 'image', None)
        if image is None or not hasattr(image, 'shape'):
            return False
        for side in self.DETAIL_PANEL_SIDES:
            frame = getattr(self, f'F_SHIKIGAMI_DETAILS_{side}', None)
            if frame is None:
                continue
            try:
                _, targets = frame.range_targets(image.shape, allow_missing_image=True)
            except (TypeError, ValueError) as error:
                logger.warning(f'DraftDuel 详情帧不可用：{side} {error}')
                continue
            for index in self.DETAIL_PANEL_RANGE_INDEXES:
                if index >= len(targets):
                    break
                try:
                    value = targets[index].ocr(image)
                except Exception as error:  # OCR 引擎异常不应让整轮选人中断
                    logger.warning(f'DraftDuel 详情区间 OCR 失败：{side}#{index} {error}')
                    break
                if not value:
                    break
            else:
                return True
        return False

    def _wait_detail_panel_open(self, timeout=None) -> bool:
        """点击后短轮询等面板出现；等待期间不再点击，避免重复触发别的弹窗。"""
        timeout = self.DETAIL_OPEN_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.DETAIL_OPEN_POLL_INTERVAL)
            self.screenshot()
            if self._detail_panel_open():
                return True

    def _capture_selection_details(self, number) -> bool:
        """每次选人展开所有详情：已展开立即返回，只有未展开才允许再次点击。"""
        captured = getattr(self, '_draft_detail_capture_rounds', None)
        if captured is None:
            captured = self._draft_detail_capture_rounds = set()
        if number in captured:
            return True
        deadline = time.monotonic() + 10
        clicks = 0
        while True:
            self.device.stuck_record_clear()
            self.screenshot()
            # 阶段或选人次数变化时，不能把下一阶段的画面当作本次详情。
            if (not self._is_draft_selection_page() or
                    self._parse_select_number(self.O_SELECT_NUMBER.ocr(self.device.image)) != number):
                return False
            if self._detail_panel_open():
                captured.add(number)
                return True
            if clicks >= self.DETAIL_CLICK_LIMIT:
                logger.warning(f'第{number}名详情连点 {self.DETAIL_CLICK_LIMIT} 次仍未展开，'
                               f'停止点击以免触发多余弹窗')
                return False
            if time.monotonic() >= deadline:
                logger.warning(f'第{number}名详情超过 10s 未就绪')
                return False
            # 图标不在时可能是展开动画未完成，也可能是面板/弹窗已挡住入口。
            # 这种情况只等待不点击，避免盲点踩到别的控件再弹一层。
            if not self.appear(self.I_SHIKIGAMI_DETAILS):
                time.sleep(self.DETAIL_OPEN_POLL_INTERVAL)
                continue
            self.appear_then_click(self.I_SHIKIGAMI_DETAILS, interval=0.3)
            clicks += 1
            if self._wait_detail_panel_open():
                captured.add(number)
                return True
            time.sleep(self.DETAIL_OPEN_POLL_INTERVAL)

    def _selection_cooperation_enabled(self):
        settings = getattr(getattr(self, 'config', None), 'draft_duel', None)
        return bool(settings and settings.draft_duel_config.match_mode != DraftMatchMode.SOLO and
                    settings.invite_config.cooperation_script.strip())

    @staticmethod
    def _parse_select_number(text):
        text = re.sub(r'\s+', '', text or '')
        match = re.search(r'请选择第([1-5一二三四五])名(?:角色|式神)', text)
        if not match:
            return None
        return dict(zip('12345一二三四五', (1, 2, 3, 4, 5) * 2))[match[1]]

    def _log_selection_wait(self, reason):
        """等待原因变化时记录一次，避免静默等待和逐帧刷屏。"""
        if getattr(self, '_draft_selection_wait_reason', None) != reason:
            logger.info(f'DraftDuel 选人等待：{reason}')
            self._draft_selection_wait_reason = reason

    def _normalize_selection_candidates(self, number, raw_names):
        """同一轮同一位置连续两帧未知后，才允许排除该候选。"""
        if getattr(self, '_draft_candidate_round', None) != number:
            self._draft_candidate_round = number
            self._draft_candidate_unknown_counts = [0, 0, 0]
        names = [normalize_name(name) for name in raw_names]
        counts = self._draft_candidate_unknown_counts
        for index, name in enumerate(names):
            counts[index] = 0 if name else counts[index] + 1
        ready = all(name or counts[index] >= 2 for index, name in enumerate(names))
        return names, ready

    def _select_shikigami(self) -> bool:
        """按轮次选人，空位消失确认成功；仅进入等待匹配，不接入战斗。"""
        if not hasattr(self, 'draft_selected'):
            self.draft_selected = []
        confirmed = set(range(1, len(self.draft_selected) + 1))
        saved = getattr(self, '_draft_selection_pending', None)
        pending = (saved[0], saved[1], getattr(self, f'I_SELECT_{saved[2]}')) if saved else None
        absent_frames = 0
        last_click = 0
        self._draft_candidate_round = None
        sampled_round = None
        sampled_templates = {}
        deadline = time.monotonic() + 600
        while True:
            self.device.stuck_record_clear()
            self.screenshot()
            self._observe_duel_lobby()
            cooperating = self._selection_cooperation_enabled()
            if cooperating:
                self._sync_draft_state(allow_partial=True)
                # 「转移队长」按钮只说明本机当前是队长，不能当队长离队判据：
                # 2026-11-18 实测队友退队后本机继承队长，该按钮反而出现。
                # 队友是否离队只认座位形态（灰头像+右下角齿轮）。
                if self._teammate_seat_quit(False):
                    self._leave_draft_team('协同方已退出队伍', success=False)
                    return False
            self._save_draft_checkpoint('select')
            if self._is_draft_rank_page():
                # 最后一选可能直接切页，来不及在选人页观察两帧空位消失。
                # 明确进入等待匹配且本轮空位消失时，补记已点击的候选。
                if pending:
                    round_number, name, _ = pending
                    if not self.appear(getattr(self, f'I_SELECT_NONE_{round_number}')):
                        confirmed.add(round_number)
                        self.draft_selected.append(name)
                        self._draft_selection_pending = None
                        self._save_draft_checkpoint('rank')
                        logger.info(f'第{round_number}名在房间页确认：{name}')
                logger.info('DraftDuel 进入选人结束房间页(post_draft_rank)')
                return True
            if time.monotonic() >= deadline:
                logger.warning('DraftDuel 选人 600s 无进展')
                return False
            if not self._is_draft_selection_page():
                self._draft_candidate_round = None
                self._log_selection_wait('未识别到选人阶段标志或备用选择按钮')
                time.sleep(0.3)
                continue
            raw_number = self.O_SELECT_NUMBER.ocr(self.device.image)
            number = self._parse_select_number(raw_number)
            if pending:
                round_number, name, button = pending
                empty = getattr(self, f'I_SELECT_NONE_{round_number}')
                if not self.appear(empty):
                    absent_frames += 1
                    if absent_frames >= 2:
                        confirmed.add(round_number)
                        self.draft_selected.append(name)
                        logger.info(f'第{round_number}名已确认：{name}')
                        pending = None
                        self._draft_selection_pending = None
                        self._save_draft_checkpoint('select')
                        deadline = time.monotonic() + 600
                else:
                    absent_frames = 0
                    if number == round_number and time.monotonic() - last_click >= 2:
                        if self.appear_then_click(button, interval=1):
                            last_click = time.monotonic()
                time.sleep(0.3)
                continue
            if number is None or number in confirmed:
                self._draft_candidate_round = None
                self._log_selection_wait(f'轮次文字={raw_number!r}，解析结果={number}，已选轮次={sorted(confirmed)}')
                time.sleep(0.3)
                continue
            if number != len(self.draft_selected) + 1:
                self._draft_candidate_round = None
                self._log_selection_wait(f'识别到第{number}名，记录中已选{len(self.draft_selected)}名，等待轮次一致')
                # 不跳轮记录，确保五个名字与头像位置始终一一对应。
                time.sleep(0.3)
                continue
            # 必须先看到本轮空位，避免队友等待期间误点旧候选。
            if not self.appear(getattr(self, f'I_SELECT_NONE_{number}')):
                self._draft_candidate_round = None
                self._log_selection_wait(f'第{number}名空位 select_none_{number} 未识别')
                time.sleep(0.3)
                continue
            if not self._capture_selection_details(number):
                time.sleep(0.3)
                continue
            from tasks.DraftDuel.strategy.panel_weights import DETAIL_FRAMES, read_panel
            if sampled_round != number:
                sampled_round = number
                sampled_templates.clear()
            cached = getattr(self, '_draft_panel_snapshot', None)
            if cached and cached[0] == number:
                panels = cached[1]
            else:
                panels = []
                for side in DETAIL_FRAMES:
                    frame = getattr(self, f'F_SHIKIGAMI_DETAILS_{side}', None)
                    panels.append(read_panel(self.runframe(frame), sampled_templates.setdefault(side, {}))
                                  if frame is not None and isinstance(self.device.image, np.ndarray) else None)
                if all(panel and panel['template_key'] for panel in panels):
                    self._draft_panel_snapshot = (number, panels)
            raw_names = [panel['raw_name'] if panel and panel['name'] else
                         getattr(self, f'O_SHIKIGAMI_NAME_{i}').ocr(self.device.image)
                         for i, panel in enumerate(panels, 1)]
            names, ready = self._normalize_selection_candidates(number, raw_names)
            if not ready or not any(names):
                self._log_selection_wait(f'候选 OCR={raw_names!r}，归一化={names!r}，'
                                         f'连续未知次数={self._draft_candidate_unknown_counts}')
                time.sleep(0.3)
                continue
            if not all(names):
                logger.info(f'DraftDuel 排除连续两次未识别的候选：'
                            f'OCR={raw_names!r}, 归一化={names!r}')
            keys = [panel['template_key'] if panel and panel['name'] == name else None
                    for name, panel in zip(names, panels)]
            template_keys = dict(zip(names, keys)) if any(keys) else {}
            try:
                if cooperating:
                    pick = (self._captain_selection(number, names, template_keys=template_keys)
                            if template_keys else self._captain_selection(number, names))
                    if pick is None:
                        self._log_selection_wait('等待双方本轮候选及队长选卡指令')
                        time.sleep(0.3)
                        continue
                else:
                    from tasks.DraftDuel.strategy.runtime_policy import choose_pick as runtime_pick
                    pick = runtime_pick(self, [name for name in names if name], team=self.draft_selected,
                                        **({'template_keys': template_keys} if template_keys else {}))
            except ValueError as exc:
                logger.warning(f'DraftDuel 候选无法选择：{exc}')
                time.sleep(0.3)
                continue
            button = getattr(self, f'I_SELECT_{names.index(pick.name) + 1}')
            logger.info(f'DraftDuel 第{number}名：{names} -> {pick.name}，得分={pick.score}')
            logger.info(f'选人理由：{"; ".join(pick.reasons)}')
            if self.appear_then_click(button, interval=1):
                self._draft_selection_wait_reason = None
                pending = (number, pick.name, button)
                self._draft_selection_pending = (number, pick.name, names.index(pick.name) + 1)
                # 记下这一轮的三选一，供真实对局训练使用（重试会覆盖同一轮，不会重复）
                if not hasattr(self, '_draft_transcript'):
                    self._draft_transcript = {}
                self._draft_transcript[number] = {
                    'round': number,
                    'offers': list(names),
                    'raw_offers': list(raw_names),
                    'pick': pick.name,
                    'pick_slot': names.index(pick.name) + 1,
                    'team': list(self.draft_selected),
                }
                if any(panels):
                    self._draft_transcript[number]['panels'] = panels
                    self._draft_transcript[number]['template_key'] = template_keys.get(pick.name)
                self._save_draft_checkpoint('select')
                absent_frames = 0
                last_click = time.monotonic()
            time.sleep(0.3)
