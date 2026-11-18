"""MetaDemon 设置与决策的离线校验。

覆盖四块，全部不碰设备、不跑状态机：
1. 配置层：两段式御魂分组、快速上阵抑制高星预设、旧配置迁移、强力追击开关、
   补票任务映射、已下线模式容错、驻留默认值、设置面板字段集合；
2. 强力追击：好友副本 / 自家鬼王两种目标态与跨场粘滞的校准口径；
3. 疲劳闸门与驻留：三分支放行/结束/驻留、驻留进入与复核；
4. 票务三态：UNKNOWN 不驻留、补票用尽、成功交棒。

2026-11-10 重建说明：原版 526 行绑定「内置刷票」——`ticket_farming_config` /
`AwakeTicketTask` / `ExplorationTicketTask` / `run_ticket_farming` /
`tickets_full_before_farming` / `ticket_farming_battle_conf` /
`MetaDemonMode.AWAKE|EXPLORATION` / `ExplorationTarget`。内置刷票已下线、相应模块
已删除（票的刷取统一由独立任务「鬼王门票」承担），因此那些用例连同被测对象一起
作废；本版只保留仍然有效的断言，并补上驻留等待与好友副本强力追击。

运行（仓库根目录）：
    .venv/Scripts/python.exe -m unittest dev_tools.check_meta_demon_settings -v
"""
from datetime import datetime, timedelta
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module.config.config_model import ConfigModel
from module.exception import TaskEnd
from tasks.MetaDemon.boss_battle import BossBattle
from tasks.MetaDemon.config import BossType, MetaDemon, MetaDemonMode, RefillTask, loadout_group
from tasks.MetaDemon.queue import RefillOutcome


def _battle(conf=None, *, friend=False, star=None):
    """构造一个不经 __init__ 的 BossBattle，设备相关方法全部打桩。"""
    owner = object.__new__(BossBattle)
    owner.conf = conf if conf is not None else MetaDemon()
    owner.cur_boss_type = star
    owner._friend_mode = friend
    owner._friend_block_until = 0.0
    owner._idle_deadline = None
    owner._idle_reason = None
    owner._idle_next_resume = 0.0
    owner._idle_next_deadline_check = 0.0
    owner.total_count = 0
    owner.screenshot = Mock()
    owner.appear = Mock(return_value=False)
    owner.ui_click_until_disappear = Mock(return_value=True)
    owner.set_next_run = Mock()
    owner.finish_task = Mock(side_effect=TaskEnd)
    owner.State = SimpleNamespace(done=False)
    return owner


def _powerful_control(owner, enabled):
    """把强力追击控件伪装成指定状态：True=已开启(蓝圈)，False=未开启(暗环)，None=读不到。"""

    def appear(rule, **kwargs):
        if rule is owner.I_MD_ENABLE_POWERFUL_FIRE:
            return enabled is True
        if rule is owner.I_MD_DISABLE_POWERFUL_FIRE:
            return enabled is False
        return False

    owner.appear = Mock(side_effect=appear)


class MetaDemonSettingsChecks(unittest.TestCase):
    # ---------------------------------------------------------------- 配置层

    def test_loadout_group_collapses_to_two_categories(self):
        self.assertEqual([loadout_group(BossType(i)) for i in range(1, 7)],
                         ['low_star'] * 4 + ['high_star'] * 2)
        conf = MetaDemon()
        # 三段式的票务战斗配置随内置刷票一起下线，字段与属性都不该再存在
        self.assertNotIn('ticket_farming_battle_conf', MetaDemon.model_fields)
        self.assertFalse(hasattr(conf, 'ticket_farming_battle_conf'))
        self.assertFalse(hasattr(conf, 'preset_config'))
        self.assertEqual(set(MetaDemon.model_fields),
                         {'scheduler', 'meta_demon_config', 'general_config', 'switch_soul_config',
                          'quick_preset_config', 'assistance_config',
                          'low_star_battle_conf', 'high_star_battle_conf'})

    def test_quick_loadout_suppresses_high_preset_only(self):
        conf = MetaDemon(general_config={'high_star_quick_loadout_enable': True},
                         low_star_battle_conf={'preset_enable': True},
                         high_star_battle_conf={'preset_enable': True, 'green_enable': True})
        self.assertTrue(conf.battle_config('low_star').preset_enable)
        # 高星交给快速上阵，战斗内预设必须关掉
        self.assertFalse(conf.battle_config('high_star').preset_enable)
        self.assertTrue(conf.battle_config('high_star').green_enable)
        # 只影响取出的视图，不改写用户配置
        self.assertTrue(conf.high_star_battle_conf.preset_enable)

    def test_legacy_six_star_souls_migrate_into_two_categories(self):
        conf = MetaDemon(switch_soul={'enable': True, 'one_star': '2,3', 'two_stars': '3,4',
                                      'five_stars': '活动,高星', 'enable_one_star_preset': True})
        low = conf.switch_soul_config.switch_for('low_star')
        high = conf.switch_soul_config.switch_for('high_star')
        self.assertTrue(low.enable)
        self.assertEqual(low.switch_group_team, '2,3')
        self.assertTrue(high.enable_switch_by_name)
        self.assertEqual((high.group_name, high.team_name), ('活动', '高星'))
        self.assertTrue(conf.low_star_battle_conf.preset_enable)
        self.assertNotIn('switch_soul', conf.model_dump())

    def test_powerful_friend_boss_is_independent_of_star_list(self):
        self.assertFalse(MetaDemon().meta_demon_config.powerful_friend_boss)
        conf = MetaDemon(meta_demon_config={'powerful_list': '4 5'})
        self.assertEqual(conf.meta_demon_config.powerful_list_v,
                         [BossType.FOUR_STARS, BossType.FIVE_STARS])
        # 只配星级列表不会顺带打开好友副本开关，反之亦然
        self.assertFalse(conf.meta_demon_config.powerful_friend_boss)
        friend_only = MetaDemon(meta_demon_config={'powerful_friend_boss': True})
        self.assertEqual(friend_only.meta_demon_config.powerful_list_v, [])
        self.assertTrue(friend_only.meta_demon_config.powerful_friend_boss)

    def test_refill_task_ticket_maps_to_standalone_task(self):
        self.assertEqual(RefillTask.TICKET.value, '鬼王门票')
        mapping = {
            RefillTask.TICKET: 'MetaDemonTicket',
            RefillTask.EXPLORATION: 'Exploration',
            RefillTask.AWAKEN: 'EvoZone',
            RefillTask.OROCHI: 'Orochi',
            RefillTask.BONDLING: 'BondlingFairyland',
            RefillTask.DISABLE: None,
        }
        for refill, expected in mapping.items():
            with self.subTest(refill=refill):
                conf = MetaDemon(meta_demon_config={'refill_task': refill})
                self.assertEqual(conf.meta_demon_config.refill_task_v, expected)

    def test_parse_modes_drops_offline_modes_without_raising(self):
        # 已下线的刷票模式残留值必须丢弃告警，不能抛错（抛错会让整份配置加载失败）
        conf = MetaDemon(meta_demon_config={'mode': ['鬼王战斗', '门票刷取：觉醒']})
        self.assertEqual(conf.meta_demon_config.mode, [MetaDemonMode.BOSS_BATTLE])
        legacy = MetaDemon(meta_demon_config={'mode': ['门票刷取：探索绘卷']})
        self.assertEqual(legacy.meta_demon_config.mode, [MetaDemonMode.BOSS_BATTLE])
        # 两者互斥仍然生效（这是用户配置错误，必须显式报错，不能静默取其一）
        with self.assertRaises(ValueError):
            MetaDemon(meta_demon_config={'mode': ['鬼王战斗', '鬼王协助']})

    def test_idle_wait_defaults(self):
        cfg = MetaDemon().meta_demon_config
        self.assertFalse(cfg.idle_wait_enable)
        self.assertEqual(cfg.idle_wait_buffer_min, 3)
        self.assertEqual(cfg.idle_poll_interval, 8)
        self.assertEqual(cfg.idle_battle_min_remain, 4)

    def test_settings_payload_groups(self):
        payload = ConfigModel.script_task(SimpleNamespace(meta_demon=MetaDemon()), 'MetaDemon')
        self.assertEqual(set(payload), {'scheduler', 'meta_demon_config', 'general_config',
                                        'switch_soul_config', 'quick_preset_config',
                                        'assistance_config', 'low_star_battle_conf',
                                        'high_star_battle_conf'})
        for name, group in payload.items():
            with self.subTest(group=name):
                self.assertIsInstance(group, list)
                for field in group:
                    self.assertIn('default', field)

    # ------------------------------------------------------------ 强力追击

    def test_powerful_own_boss_in_list_opens(self):
        owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}),
                        star=BossType.FIVE_STARS)
        _powerful_control(owner, enabled=False)
        self.assertTrue(owner.ensure_powerful_before_battle())
        owner.ui_click_until_disappear.assert_called_once()
        args, kwargs = owner.ui_click_until_disappear.call_args
        # 开启方向点「暗环」本身，不依赖蓝圈对勾是否命中
        self.assertIs(args[0], owner.I_MD_DISABLE_POWERFUL_FIRE)
        self.assertEqual(kwargs['max_clicks'], 2)
        self.assertEqual(kwargs['timeout'], 6)
        self.assertEqual(kwargs['confirm_frames'], 2)
        self.assertNotIn('guard', kwargs)

    def test_powerful_own_boss_not_in_list_closes(self):
        owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}),
                        star=BossType.THREE_STARS)
        _powerful_control(owner, enabled=True)
        self.assertFalse(owner.ensure_powerful_before_battle())
        args, _ = owner.ui_click_until_disappear.call_args
        self.assertIs(args[0], owner.I_MD_ENABLE_POWERFUL_FIRE)

    def test_powerful_friend_boss_forced_on_ignoring_star(self):
        conf = MetaDemon(meta_demon_config={'powerful_list': '4 5', 'powerful_friend_boss': True})
        # 好友副本星级界面上不可见，cur_boss_type 是上一只自家鬼王的残留值
        owner = _battle(conf, friend=True, star=BossType.THREE_STARS)
        _powerful_control(owner, enabled=False)
        self.assertTrue(owner.ensure_powerful_before_battle())
        args, _ = owner.ui_click_until_disappear.call_args
        self.assertIs(args[0], owner.I_MD_DISABLE_POWERFUL_FIRE)

    def test_powerful_friend_boss_off_does_not_intervene(self):
        owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}), friend=True)
        _powerful_control(owner, enabled=True)
        self.assertIsNone(owner.ensure_powerful_before_battle())
        owner.ui_click_until_disappear.assert_not_called()

    def test_powerful_already_matched_costs_no_click(self):
        owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}),
                        star=BossType.FIVE_STARS)
        _powerful_control(owner, enabled=True)
        self.assertTrue(owner.ensure_powerful_before_battle())
        owner.ui_click_until_disappear.assert_not_called()

    def test_powerful_unknown_control_keeps_battle_running(self):
        owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}),
                        star=BossType.FIVE_STARS)
        _powerful_control(owner, enabled=None)
        self.assertIsNone(owner.ensure_powerful_before_battle())
        owner.ui_click_until_disappear.assert_not_called()

    def test_powerful_switch_failure_reports_current_state(self):
        for want_on, current in ((True, False), (False, True)):
            with self.subTest(want_on=want_on):
                owner = _battle(MetaDemon(meta_demon_config={'powerful_list': '4 5'}),
                                star=BossType.FIVE_STARS if want_on else BossType.THREE_STARS)
                _powerful_control(owner, enabled=current)
                owner.ui_click_until_disappear = Mock(return_value=False)
                # 切换失败时保持原状态继续挑战，不阻断战斗
                self.assertEqual(owner.ensure_powerful_before_battle(), current)

    # -------------------------------------------------------- 疲劳闸门与驻留

    def test_fatigue_not_full_passes(self):
        owner = _battle()
        owner._fatigue_full = Mock(return_value=False)
        self.assertTrue(owner.check_fatigue())
        owner.set_next_run.assert_not_called()

    def test_fatigue_full_without_auto_tea_ends_task(self):
        owner = _battle(MetaDemon(meta_demon_config={'auto_tea': False}))
        owner._fatigue_full = Mock(return_value=True)
        with self.assertRaises(TaskEnd):
            owner.check_fatigue()
        owner.set_next_run.assert_called_once()
        self.assertFalse(owner.State.done)

    def test_fatigue_full_with_auto_tea_enters_idle_once(self):
        conf = MetaDemon(meta_demon_config={'auto_tea': True, 'idle_wait_enable': True})
        owner = _battle(conf)
        owner._fatigue_full = Mock(return_value=True)
        deadline = datetime.now() + timedelta(minutes=10)
        owner._next_task_start = Mock(return_value=deadline)
        # 首次：转入驻留，本拍不接战
        self.assertFalse(owner.check_fatigue())
        self.assertEqual(owner._idle_reason, 'fatigue')
        self.assertEqual(owner._idle_deadline, deadline)
        # 已驻留：放行接战 —— 喝茶窗口只在点「挑战」时弹出，禁止接战就永远喝不到茶
        self.assertTrue(owner.check_fatigue())
        owner.set_next_run.assert_not_called()

    def test_fatigue_full_with_auto_tea_but_idle_disabled_passes(self):
        owner = _battle(MetaDemon(meta_demon_config={'auto_tea': True, 'idle_wait_enable': False}))
        owner._fatigue_full = Mock(return_value=True)
        self.assertTrue(owner.check_fatigue())
        self.assertIsNone(owner._idle_deadline)

    def test_idle_enter_requires_switch_and_deadline(self):
        disabled = _battle(MetaDemon(meta_demon_config={'idle_wait_enable': False}))
        self.assertFalse(disabled._enter_idle_wait('tickets'))
        self.assertIsNone(disabled._idle_deadline)

        no_task = _battle(MetaDemon(meta_demon_config={'idle_wait_enable': True}))
        no_task._next_task_start = Mock(return_value=None)
        self.assertFalse(no_task._enter_idle_wait('tickets'))
        self.assertIsNone(no_task._idle_deadline)

    def test_idle_reentry_only_tightens_deadline(self):
        owner = _battle(MetaDemon(meta_demon_config={'idle_wait_enable': True}))
        far = datetime.now() + timedelta(minutes=30)
        near = datetime.now() + timedelta(minutes=5)
        owner._next_task_start = Mock(return_value=far)
        self.assertTrue(owner._enter_idle_wait('sequence'))
        self.assertEqual(owner._idle_reason, 'sequence')
        self.assertGreater(owner._idle_next_resume, 0)
        # 更早的任务起点：收紧
        owner._next_task_start = Mock(return_value=near)
        self.assertTrue(owner._enter_idle_wait('fatigue'))
        self.assertEqual(owner._idle_deadline, near)
        # 触发源不被覆盖，否则票尽与疲劳会互相改写复核策略
        self.assertEqual(owner._idle_reason, 'sequence')
        # 更晚的任务起点：不延长
        owner._next_task_start = Mock(return_value=far + timedelta(minutes=30))
        self.assertTrue(owner._enter_idle_wait('tickets'))
        self.assertEqual(owner._idle_deadline, near)

    def test_idle_resume_by_reason(self):
        owner = _battle(MetaDemon(meta_demon_config={'idle_wait_enable': True}),
                        star=BossType.TWO_STARS)
        owner._idle_deadline = datetime.now() + timedelta(minutes=10)

        owner._idle_reason = 'fatigue'
        owner._fatigue_full = Mock(return_value=True)
        self.assertFalse(owner._idle_try_resume())
        owner._fatigue_full = Mock(return_value=False)
        self.assertTrue(owner._idle_try_resume())
        self.assertIsNone(owner._idle_reason)
        self.assertIsNone(owner._idle_deadline)

        owner._idle_deadline = datetime.now() + timedelta(minutes=10)
        owner._idle_reason = 'sequence'
        owner._idle_next_resume = 0.0
        self.assertTrue(owner._idle_try_resume())
        owner._idle_deadline = datetime.now() + timedelta(minutes=10)
        owner._idle_reason = 'sequence'
        owner._idle_next_resume = 9e18
        self.assertFalse(owner._idle_try_resume())

        # 票尽没有恢复来源，永不复核
        owner._idle_reason = 'tickets'
        owner._idle_next_resume = 0.0
        self.assertFalse(owner._idle_try_resume())
        self.assertIsNotNone(owner._idle_deadline)

    def test_start_battle_clears_friend_mode_after_settlement(self):
        owner = _battle(MetaDemon(), friend=True, star=BossType.FOUR_STARS)
        owner.is_in_battle = Mock(return_value=True)
        owner.run_general_battle = Mock()
        owner.ensure_powerful_before_battle = Mock()
        owner.start_battle()
        # 好友标志必须清掉：否则下一场自家鬼王会被当成好友副本
        self.assertFalse(owner._friend_mode)
        self.assertEqual(owner.total_count, 1)
        owner.run_general_battle.assert_called_once()

    # ------------------------------------------------------------ 票务三态

    def test_refill_unknown_does_not_idle(self):
        from tasks.MetaDemon.queue import DropStatus
        owner = _battle()
        owner.judge_drop_progress = Mock(return_value=(DropStatus.UNKNOWN, -1, -1))
        owner.close_summon_panel = Mock()
        owner.config = SimpleNamespace(task_call=Mock())
        owner.refill_task = Mock(return_value='MetaDemonTicket')
        self.assertEqual(owner.try_refill_ticket(), RefillOutcome.UNKNOWN)
        owner.close_summon_panel.assert_called_once()
        owner.config.task_call.assert_not_called()
        self.assertFalse(owner.State.done)

    def test_refill_exhausted_without_usable_task(self):
        from tasks.MetaDemon.queue import DropStatus
        owner = _battle()
        owner.judge_drop_progress = Mock(return_value=(DropStatus.NOT_FULL, 49, 50))
        owner.close_summon_panel = Mock()
        owner.config = SimpleNamespace(task_call=Mock())
        owner.refill_task = Mock(return_value=None)  # 未配置或调度器未启用
        self.assertEqual(owner.try_refill_ticket(), RefillOutcome.EXHAUSTED)
        owner.config.task_call.assert_not_called()
        self.assertFalse(owner.State.done)

    def test_refill_not_full_delegates_and_marks_done(self):
        from tasks.MetaDemon.queue import DropStatus
        owner = _battle()
        owner.judge_drop_progress = Mock(return_value=(DropStatus.NOT_FULL, 20, 50))
        owner.close_summon_panel = Mock()
        owner.config = SimpleNamespace(task_call=Mock())
        owner.refill_task = Mock(return_value='MetaDemonTicket')
        self.assertEqual(owner.try_refill_ticket(), RefillOutcome.DELEGATED)
        # 先关弹窗再交棒，避免收尾导航被弹窗挡着
        owner.close_summon_panel.assert_called_once()
        owner.config.task_call.assert_called_once_with('MetaDemonTicket')
        self.assertTrue(owner.State.done)

    # ---------------------------------------------------------------- 其他

    def test_disabled_souls_do_not_navigate(self):
        owner = _battle(MetaDemon(), star=BossType.ONE_STAR)
        owner.goto_page = Mock()
        self.assertTrue(owner.check_and_switch_soul())
        owner.goto_page.assert_not_called()


if __name__ == '__main__':
    unittest.main()
