# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import timedelta, time
from enum import IntEnum, Enum

from pydantic import Field, field_validator, model_validator

from module.config.multi_select import normalize_multi_select
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.config_base import ConfigBase, TimeDelta, Time, dynamic_hide
from tasks.Component.config_scheduler import Scheduler


class BossType(IntEnum):
    ONE_STAR = 1
    TWO_STARS = 2
    THREE_STARS = 3
    FOUR_STARS = 4
    FIVE_STARS = 5
    SIX_STARS = 6
    MONEY = 7


# 日志用中文星级名，避免输出 one_star 这类枚举名
BOSS_TYPE_CN = {
    BossType.ONE_STAR: '一星',
    BossType.TWO_STARS: '二星',
    BossType.THREE_STARS: '三星',
    BossType.FOUR_STARS: '四星',
    BossType.FIVE_STARS: '五星',
    BossType.SIX_STARS: '六星',
    BossType.MONEY: '氪金',
}


def boss_cn(boss_type: BossType | None) -> str:
    """鬼王类型的中文名，未知返回「未知」。"""
    if boss_type is None:
        return '未知'
    return BOSS_TYPE_CN.get(boss_type, boss_type.name)


class MetaDemonMode(str, Enum):
    BOSS_BATTLE = '鬼王战斗'
    BOSS_ASSIST = '鬼王协助'
    AWAKE = '门票刷取：觉醒七层'
    EXPLORATION = '门票刷取：探索'


class RefillTask(str, Enum):
    """掉落进度(x/50)未满时，转去刷本补票的任务（单选）。"""
    DISABLE = '不转刷'
    EXPLORATION = '探索'
    AWAKEN = '觉醒'
    OROCHI = '御魂八岐大蛇'
    BONDLING = '契灵之境'


# 御魂与战斗参数的组别（三段式）：1-4 星归低星，5-6 星与氪金归高星，刷票任务独占一段
LOADOUT_CATEGORIES = ('low_star', 'high_star', 'ticket_farming')

# 旧六段式配置里，每个组别取哪个星级字段作为迁移来源（按顺序取第一个非空）
LEGACY_STAR_FIELDS = {
    'low_star': ('one_star', 'two_stars', 'three_stars', 'four_stars'),
    'high_star': ('five_stars', 'six_stars'),
}


def loadout_group(boss_type: BossType | None) -> str:
    """星级收敛：1-4 星 → low_star，5-6 星与氪金 → high_star。

    未识别星级按 low_star 处理，与 self 版一致；它决定了御魂分组、
    战斗记录 key 与战斗参数组别的取值口径。
    """
    if boss_type is None:
        return 'low_star'
    return 'low_star' if int(boss_type) <= 4 else 'high_star'


class ExplorationTarget(str, Enum):
    ALL = '全部'
    COIN = '金币'
    EXP = '经验'
    DARUMA = '达摩'


class MetaDemonConfig(ConfigBase):
    mode: list[MetaDemonMode] = Field(
        default=[MetaDemonMode.BOSS_BATTLE], title='运行模式',
        json_schema_extra={'ui_exclusive_groups': [['门票刷取：觉醒七层', '门票刷取：探索'], ['鬼王战斗', '鬼王协助']]},
        description='勾选执行；两种门票刷取至多选一种；鬼王战斗与鬼王协助至多选一种，门票刷取优先执行')
    limit_time: Time = Field(default=Time(minute=45), description='limit_time_help')
    limit_count: int = Field(default=50, description='limit_count_help')
    auto_tea: bool = Field(default=False, description='疲劳度满时是否自动喝茶, 关闭则自动根据调度器等待间隔时间安排下一次调度')
    fire_sequence: str = Field(default='1 2 3 4', description='攻击顺序:从左到右,空格分隔(1:一星鬼王,2:二星鬼王...)\n例:3 2(先攻击三星鬼王然后二星鬼王)')
    powerful_list: str = Field(default='', description='开启强力鬼王列表(空格分隔),例:4 5(四星鬼王和五星鬼王开启强力追击)')
    synthesis_list: str = Field(default='1', description='鬼王合成列表(空格分隔),支持1-5星合成,例:1 2 3(合成一星,二星,三星鬼王)')
    refill_task: RefillTask = Field(default=RefillTask.DISABLE, description='未勾选门票刷取模式时的兜底补票：掉落进度(x/50)未满且无鬼王可打时,转去刷本补票的任务(单选);所选任务的调度器未启用时按原逻辑退出')
    idle_wait_enable: bool = Field(default=False, description='无法继续战斗时不结束任务,驻留鬼王界面等待:票尽且补票用尽/门票星级与攻击顺序无交集/疲劳满且已开自动喝茶;期间轮询好友分享的鬼王并接战,疲劳或门票星级恢复后自动回到正常流程,直到下一任务开始前3分钟(没有其他启用任务时直到限时)')
    idle_wait_buffer_min: int = Field(default=3, ge=0, le=30, description='驻留提前收尾余量(分钟):下一任务开始前该分钟数结束驻留')
    idle_poll_interval: int = Field(default=8, ge=2, le=60, description='驻留期间轮询好友分享鬼王的间隔(秒)')
    idle_battle_min_remain: int = Field(default=4, ge=1, le=30, description='好友分享鬼王剩余时间少于该分钟数时不接战(按单场战斗耗时估算)')

    @field_validator('mode', mode='before')
    @classmethod
    def parse_modes(cls, value):
        """兼容单选字符串 / JSON 列表 / 历史选项名，并校验互斥组。"""
        selected = [MetaDemonMode('门票刷取：探索' if item == '门票刷取：探索绘卷' else item)
                    for item in normalize_multi_select(value)]
        tickets = [item for item in (MetaDemonMode.AWAKE, MetaDemonMode.EXPLORATION) if item in selected]
        if len(tickets) > 1:
            raise ValueError('觉醒和探索门票刷取至多选择一种')
        bosses = [item for item in (MetaDemonMode.BOSS_BATTLE, MetaDemonMode.BOSS_ASSIST) if item in selected]
        if len(bosses) > 1:
            raise ValueError('鬼王战斗和鬼王协助至多选择一种')
        # 门票刷取在前：分派层按列表顺序执行，先刷满票再进战斗
        return tickets + bosses

    @property
    def limit_time_v(self) -> timedelta:
        if isinstance(self.limit_time, time):
            return timedelta(hours=self.limit_time.hour, minutes=self.limit_time.minute, seconds=self.limit_time.second)
        return self.limit_time

    @property
    def fire_sequence_v(self) -> list[BossType]:
        sequence_text = self.fire_sequence.strip()
        if sequence_text == '':
            return []
        fire_num_list = sequence_text.split(' ')
        fire_boss_type_list = [BossType(int(num_text)) for num_text in fire_num_list]
        return fire_boss_type_list

    @property
    def powerful_list_v(self) -> list[BossType]:
        powerful_list_text = self.powerful_list.strip()
        if powerful_list_text == '':
            return []
        powerful_num_list = powerful_list_text.split(' ')
        powerful_boss_type_list = [BossType(int(num_text)) for num_text in powerful_num_list]
        return powerful_boss_type_list

    @property
    def synthesis_list_v(self) -> list[BossType]:
        synthesis_list_text = self.synthesis_list.strip()
        if synthesis_list_text == '':
            return []
        synthesis_num_list = synthesis_list_text.split(' ')
        synthesis_boss_type_list = [BossType(int(num_text)) for num_text in synthesis_num_list]
        return synthesis_boss_type_list

    @property
    def refill_task_v(self) -> str | None:
        """补票任务的大驼峰名；未配置返回 None。"""
        return {
            RefillTask.EXPLORATION: 'Exploration',
            RefillTask.AWAKEN: 'EvoZone',
            RefillTask.OROCHI: 'Orochi',
            RefillTask.BONDLING: 'BondlingFairyland',
        }.get(self.refill_task)


class MetaDemonGeneralConfig(ConfigBase):
    high_star_quick_loadout_enable: bool = Field(
        default=False, title="高星鬼王启用快速上阵",
        description="仅五星、六星鬼王战斗生效；使用对应御魂配置中的分组名、预设名快速上阵，跳过式神录切魂与战斗准备预设。门票刷取仍按原配置执行。")
    exploration_rotate: bool = Field(
        default=False, title='探索自动添加候补式神',
        description='与探索任务的自动添加候补式神一致：启用轮换并自动补充候补式神；关闭时关闭轮换。')
    awake_buff_enable: bool = Field(default=False, title='觉醒启用加成')
    exploration_target: ExplorationTarget = Field(default=ExplorationTarget.ALL, title='探索目标')


class MetaDemonSwitchSoulConfig(ConfigBase):
    """三段式御魂配置：低星 / 高星 / 门票刷取，每段都支持按编号或按名称切换。"""

    low_star_enable: bool = Field(default=False, title='按编号切换')
    low_star_group_team: str = Field(default='-1,-1', title='分组、队号', description='switch_group_team_help')
    low_star_by_name: bool = Field(default=False, title='按名称切换', description='enable_switch_by_name_help')
    low_star_group_team_name: str = Field(default='', title='分组名、预设名')
    high_star_enable: bool = Field(default=False, title='按编号切换')
    high_star_group_team: str = Field(default='-1,-1', title='分组、队号', description='switch_group_team_help')
    high_star_by_name: bool = Field(default=False, title='按名称切换', description='enable_switch_by_name_help')
    high_star_group_team_name: str = Field(default='', title='分组名、预设名')
    ticket_farming_enable: bool = Field(default=False, title='按编号切换')
    ticket_farming_group_team: str = Field(default='-1,-1', title='分组、队号', description='switch_group_team_help')
    ticket_farming_by_name: bool = Field(default=False, title='按名称切换', description='enable_switch_by_name_help')
    ticket_farming_group_team_name: str = Field(default='', title='分组名、预设名')

    def switch_for(self, category: str):
        """把某个组别的四字段折叠成框架的 SwitchSoulConfig。"""
        from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig
        names = str(getattr(self, f'{category}_group_team_name')).split(',')
        by_name = getattr(self, f'{category}_by_name')
        group_name, team_name = (part.strip() for part in names) if len(names) == 2 else ('', '')
        numbered = getattr(self, f'{category}_enable') and not by_name
        return SwitchSoulConfig(
            enable=numbered,
            switch_group_team=getattr(self, f'{category}_group_team'),
            enable_switch_by_name=by_name, group_name=group_name, team_name=team_name,
        )


class MetaDemonQuickPresetConfig(ConfigBase):
    high_star_custom_preset_enable: bool = Field(
        default=False, title='指定高星切换预设',
        description='每次挑战新首领时上阵对应预设并装配一次御魂')
    high_star_custom_preset: str = Field(
        default='ALL:(1,1)', title='自定义配置上阵预设',
        description='示例：boss_name:("预设组","预设名"); 多项以分号分隔；ALL 表示其他怪物。')


class AssistanceRole(str, Enum):
    ASSIST = '协助'
    OFFERING = '上供'


class MetaDemonAssistanceConfig(ConfigBase):
    role: AssistanceRole = Field(
        default=AssistanceRole.OFFERING, title='鬼王协助身份',
        description='上供：仅召唤六星鬼王，分享给指定好友后等待击杀；协助暂未实现。')
    friend_names: str = Field(
        default='', title='分享好友名字',
        description='填写游戏内好友完整名字；多个名字用分号或换行分隔，按名称精确匹配，不使用近似名字。')
    poll_interval: int = Field(
        default=5, ge=2, le=60, title='击杀等待检查间隔（秒）',
        description='分享后按此间隔检查鬼王是否已被击杀，不重复分享或自行挑战。')

    @property
    def friends(self) -> list[str]:
        import re
        return list(dict.fromkeys(name.strip() for name in re.split(r'[;；\n\r]+', self.friend_names) if name.strip()))


class MetaDemonScheduler(Scheduler):
    wait_interval: TimeDelta = Field(default=TimeDelta(hours=1, minutes=40), description='疲劳度满时等多长时间后再次运行本任务,建议设置等待完全恢复疲劳度所需的时间\n例:100分钟恢复100点疲劳度,再次运行必定可以继续战斗')

    hide_fields = dynamic_hide('server_update', 'delay_date')


class MetaDemon(ConfigBase):
    scheduler: MetaDemonScheduler = Field(default_factory=MetaDemonScheduler)
    meta_demon_config: MetaDemonConfig = Field(default_factory=MetaDemonConfig)
    general_config: MetaDemonGeneralConfig = Field(default_factory=MetaDemonGeneralConfig)
    switch_soul_config: MetaDemonSwitchSoulConfig = Field(default_factory=MetaDemonSwitchSoulConfig)
    quick_preset_config: MetaDemonQuickPresetConfig = Field(default_factory=MetaDemonQuickPresetConfig)
    assistance_config: MetaDemonAssistanceConfig = Field(default_factory=MetaDemonAssistanceConfig)
    low_star_battle_conf: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)
    high_star_battle_conf: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)
    ticket_farming_battle_conf: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)

    def battle_config(self, category: str) -> GeneralBattleConfig:
        """取某个组别的战斗参数；高星启用快速上阵时关掉战斗内预设（由快速上阵负责上阵）。"""
        config = getattr(self, f'{category}_battle_conf')
        if category == 'high_star' and self.general_config.high_star_quick_loadout_enable:
            return config.model_copy(update={'preset_enable': False})
        return config

    @model_validator(mode='before')
    @classmethod
    def migrate_legacy_config(cls, data):
        """把六段式旧配置迁移成三段式。

        第一步：`switch_soul`（含 one_star..six_stars / enable_*_preset / switch_once）
        按 1-4 星归低星、5-6 星归高星折叠成 `switch_soul_config`；
        每段取第一个非空的星级字段作为代表，`switch_once` 语义由
        `_active_loadout_group` 缓存取代，直接丢弃。

        第二步：历史中间态 `preset_config` 展开成三段 `{category}_battle_conf`。

        两步合并在一个 validator 里，避免依赖多个 `mode='before'` 的隐式执行顺序。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)

        # ---- 第一步：六段式 switch_soul -> 三段式 switch_soul_config ----
        old_switch = data.pop('switch_soul', None)
        migrated_presets: dict[str, dict] = {}
        if isinstance(old_switch, dict) and 'switch_soul_config' not in data:
            souls: dict = {}
            for category, stars in LEGACY_STAR_FIELDS.items():
                star = next((key for key in stars if str(old_switch.get(key, '')).strip()), stars[0])
                text = str(old_switch.get(star, '')).strip()
                if text == '-1,-1':
                    text = ''
                parts = [part.strip() for part in text.split(',')]
                numbered = len(parts) == 2 and all(part.isdigit() for part in parts)
                souls[f'{category}_enable'] = bool(old_switch.get('enable') and text and numbered)
                souls[f'{category}_by_name'] = bool(old_switch.get('enable') and text and not numbered)
                souls[f'{category}_group_team' if numbered else f'{category}_group_team_name'] = text
                if numbered:
                    # 战斗内预设开关同样按分组折叠，星级粒度在这一维上消失
                    migrated_presets[category] = {
                        'preset_enable': bool(old_switch.get(f'enable_{star}_preset')),
                        'preset_group': min(max(int(parts[0]), 1), 7),
                        'preset_team': min(max(int(parts[1]), 1), 5),
                    }
            # ticket_farming 段没有历史配置来源，保持默认（不启用）
            data['switch_soul_config'] = souls

        # ---- 第二步：preset_config（历史中间态）-> 三段 battle_conf ----
        old_presets = data.pop('preset_config', None)
        if isinstance(old_presets, dict):
            souls = dict(data.get('switch_soul_config') or {})
            if old_presets.get('high_star_group_name') and old_presets.get('high_star_preset_name'):
                souls.setdefault('high_star_group_team_name',
                                 f"{old_presets['high_star_group_name']},{old_presets['high_star_preset_name']}")
            if souls:
                data['switch_soul_config'] = souls
            for category in LOADOUT_CATEGORIES:
                migrated_presets.setdefault(category, {
                    'preset_enable': old_presets.get(f'{category}_enable', False) if category != 'high_star' else False,
                    'preset_group': min(max(int(old_presets.get(f'{category}_group', 1)), 1), 7),
                    'preset_team': min(max(int(old_presets.get(f'{category}_team', 1)), 1), 5),
                })

        for category, conf in migrated_presets.items():
            data.setdefault(f'{category}_battle_conf', conf)
        return data
