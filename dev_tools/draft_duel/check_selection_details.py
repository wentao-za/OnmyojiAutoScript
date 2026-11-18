"""Details capture and member entry checks without device-service dependencies."""
import ast
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]


def load_class(filename, namespace):
    path = ROOT / 'tasks/DraftDuel/runtime' / filename
    namespace['__file__'] = str(path)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[cls.name]


class FakeClock:
    """时间随调用前进，保证「点击上限/轮询超时」不会变成死循环。"""

    def __init__(self, step=0.1):
        self.now = 0.0
        self.step = step
        self.sleeps = []

    def monotonic(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DetailsChecks(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = FakeClock()
        self.logger = Mock()
        cls = load_class('selection.py', dict(Path=Path, re=re, time=self.clock, logger=self.logger))
        self.cls = cls
        self.owner = cls()
        self.owner.SHIKIGAMI_DETAILS_DIR = Path(self.folder.name)
        self.owner.config = SimpleNamespace(config_name='TEST')
        self.owner.device = SimpleNamespace(image=object(), stuck_record_clear=Mock(), image_save=Mock())
        self.owner.screenshot = Mock()
        self.owner._is_draft_selection_page = Mock(return_value=True)
        self.owner.O_SELECT_NUMBER = SimpleNamespace(ocr=Mock(return_value='请选择第1名式神'))
        self.owner.I_SHIKIGAMI_DETAILS = object()
        self.owner.appear = Mock(return_value=True)
        self.owner.appear_then_click = Mock(return_value=True)
        self.owner._detail_panel_open = Mock(return_value=True)

    # ---- 打开判据 -------------------------------------------------------

    def test_panel_open_requires_life_and_defense_rows(self):
        owner = self.owner
        del owner._detail_panel_open
        owner.device.image = SimpleNamespace(shape=(720, 1280, 3))

        def frame(*values):
            targets = [SimpleNamespace(ocr=Mock(return_value=v)) for v in values]
            return SimpleNamespace(range_targets=Mock(return_value=([], targets)))

        owner.F_SHIKIGAMI_DETAILS_MEDIUM = frame(0, 15286, 496)
        self.assertTrue(owner._detail_panel_open())

        owner.F_SHIKIGAMI_DETAILS_MEDIUM = frame(0, 15286, 0)
        self.assertFalse(owner._detail_panel_open())

        # 弹窗盖住一个面板时，另一个面板仍能证明已展开。
        owner.F_SHIKIGAMI_DETAILS_RIGHT = frame(0, 24155, 1074)
        self.assertTrue(owner._detail_panel_open())

        # 未展开（雷达图）时数值行全是 0，不能误判成已打开。
        owner.F_SHIKIGAMI_DETAILS_RIGHT = frame(0, 0, 0)
        self.assertFalse(owner._detail_panel_open())

    def test_panel_open_without_image_never_opens(self):
        owner = self.owner
        del owner._detail_panel_open
        owner.device.image = None
        self.assertFalse(owner._detail_panel_open())

    def test_panel_open_survives_broken_frame(self):
        owner = self.owner
        del owner._detail_panel_open
        owner.device.image = SimpleNamespace(shape=(720, 1280, 3))
        owner.F_SHIKIGAMI_DETAILS_MEDIUM = SimpleNamespace(range_targets=Mock(side_effect=ValueError('out of range')))
        owner.F_SHIKIGAMI_DETAILS_RIGHT = SimpleNamespace(
            range_targets=Mock(return_value=([], [SimpleNamespace(ocr=Mock(side_effect=RuntimeError('ocr down')))] * 3)))
        self.assertFalse(owner._detail_panel_open())

    # ---- 点击节奏 -------------------------------------------------------

    def test_already_open_returns_without_clicking(self):
        self.assertTrue(self.owner._capture_selection_details(1))
        self.owner.appear_then_click.assert_not_called()
        self.assertEqual(self.owner.screenshot.call_count, 1)

    def test_single_click_stops_once_panel_opens(self):
        owner = self.owner
        owner._detail_panel_open = Mock(side_effect=[False, True])
        self.assertTrue(owner._capture_selection_details(1))
        self.assertEqual(owner.appear_then_click.call_count, 1)
        self.assertEqual(owner._detail_panel_open.call_count, 2)

    def test_repeated_failure_stops_at_click_limit(self):
        owner = self.owner
        owner._detail_panel_open = Mock(return_value=False)
        self.assertFalse(owner._capture_selection_details(1))
        self.assertEqual(owner.appear_then_click.call_count, owner.DETAIL_CLICK_LIMIT)
        owner.device.image_save.assert_not_called()

    def test_missing_entry_icon_never_clicks(self):
        owner = self.owner
        owner._detail_panel_open = Mock(return_value=False)
        owner.appear = Mock(return_value=False)
        self.assertFalse(owner._capture_selection_details(1))
        owner.appear_then_click.assert_not_called()

    def test_wait_polls_with_bounded_timeout(self):
        owner = self.owner
        owner._detail_panel_open = Mock(return_value=False)
        self.assertFalse(owner._wait_detail_panel_open())
        self.assertGreaterEqual(owner._detail_panel_open.call_count, 2)
        self.assertTrue(all(s == owner.DETAIL_OPEN_POLL_INTERVAL for s in self.clock.sleeps))

    def test_wait_returns_as_soon_as_panel_opens(self):
        owner = self.owner
        owner._detail_panel_open = Mock(side_effect=[False, True])
        self.assertTrue(owner._wait_detail_panel_open())
        self.assertEqual(owner._detail_panel_open.call_count, 2)

    # ---- 幂等与阶段校验 -------------------------------------------------

    def test_same_number_is_not_rechecked(self):
        owner = self.owner
        self.assertTrue(owner._capture_selection_details(1))
        calls = owner._detail_panel_open.call_count
        self.assertTrue(owner._capture_selection_details(1))
        self.assertEqual(owner._detail_panel_open.call_count, calls)

    def test_changed_stage_or_pick_does_not_capture(self):
        owner = self.owner
        owner._detail_panel_open = Mock(return_value=False)
        owner._is_draft_selection_page.return_value = False
        self.assertFalse(owner._capture_selection_details(1))
        owner._is_draft_selection_page.return_value = True
        owner.O_SELECT_NUMBER.ocr.return_value = '请选择第2名式神'
        self.assertFalse(owner._capture_selection_details(1))
        owner.device.image_save.assert_not_called()

    def test_detail_recognition_does_not_access_disk(self):
        owner = self.owner
        owner.appear = Mock(return_value=False)
        owner._detail_panel_open = Mock(return_value=True)
        owner.device.image_save.side_effect = [OSError('disk failure'), None]
        self.assertTrue(owner._capture_selection_details(1))
        owner.device.image_save.assert_not_called()

    def test_detail_constants_are_bounded(self):
        cls = self.cls
        self.assertLessEqual(cls.DETAIL_CLICK_LIMIT, 3)
        self.assertLess(cls.DETAIL_OPEN_POLL_INTERVAL, cls.DETAIL_OPEN_TIMEOUT)
        self.assertEqual(cls.DETAIL_PANEL_RANGE_INDEXES, (1, 2))


class MemberEntryChecks(unittest.TestCase):
    def test_member_waits_without_navigating_to_lobby(self):
        modes = SimpleNamespace(MEMBER='member', CAPTAIN='captain')
        cls = load_class('invitation.py', dict(DraftMatchMode=modes))
        owner = Mock()
        owner.config.draft_duel.draft_duel_config.match_mode = modes.MEMBER
        owner._goto_draft_lobby.return_value = False
        owner._wait_member_start.return_value = True
        self.assertTrue(cls._enter_matchmaking(owner))
        owner._goto_draft_lobby.assert_not_called()
        owner.prepare_battle_cooperation.assert_called_once_with('DraftDuel')
        owner._wait_member_start.assert_called_once()
        owner.appear_then_click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
