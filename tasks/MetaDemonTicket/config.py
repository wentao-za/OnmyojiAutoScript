from datetime import time, timedelta
from enum import Enum
from typing import ClassVar

from pydantic import Field

from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_settings_layout import SettingsLayoutConfig, settings_section
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralInvite.config_invite import InviteConfig
from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig
from tasks.EvoZone.config import KirinType, Layer
from tasks.Exploration.config import UserStatus


class TicketMode(str, Enum):
    AWAKE = '门票刷取：觉醒'
    EXPLORATION = '门票刷取：探索'


class ExplorationTarget(str, Enum):
    ALL = '全部'
    COIN = '金币'
    EXP = '经验'
    DARUMA = '达摩'


class TicketGeneralConfig(ConfigBase):
    mode: TicketMode = Field(default=TicketMode.AWAKE, title="刷票玩法")
    limit_time: Time = Field(default=Time(minute=45), description="limit_time_help")
    user_status: UserStatus = Field(default=UserStatus.ALONE, title='门票刷取身份')
    exploration_rotate: bool = Field(default=False, title='探索自动添加候补式神', description='与探索任务的自动添加候补式神一致：启用轮换并自动补充候补式神；关闭时关闭轮换。')
    awake_buff_enable: bool = Field(default=False, title='觉醒启用加成')
    awake_kirin_type: KirinType = Field(default=KirinType.LIGHTNINGKIRIN, title='觉醒麒麟',
        description='门票刷取：觉醒使用的麒麟类型，仅影响超鬼王门票刷取。')
    awake_layer: Layer = Field(default=Layer.SEVEN, title='觉醒层数',
        description='门票刷取：觉醒使用的层数，可选一至十层；默认七层。')
    exploration_target: ExplorationTarget = Field(default=ExplorationTarget.ALL, title='探索目标')


    @property
    def limit_time_v(self) -> timedelta:
        if isinstance(self.limit_time, time):
            return timedelta(hours=self.limit_time.hour, minutes=self.limit_time.minute, seconds=self.limit_time.second)
        return self.limit_time


class MetaDemonTicket(ConfigBase):
    settings_layout_config: SettingsLayoutConfig = Field(default_factory=SettingsLayoutConfig)
    settings_layout: ClassVar[list[dict]] = [
        settings_section('scheduler', '任务调度', groups=[{'name': 'scheduler'}]),
        settings_section('general_config', '通用设置', groups=[{'name': 'general_config'}]),
        settings_section('invite_config', '通用邀请', groups=[{'name': 'invite_config'}]),
        settings_section('switch_soul_config', '切换御魂', groups=[{'name': 'switch_soul_config'}]),
        settings_section('general_battle_config', '战斗配置', groups=[{'name': 'general_battle_config'}]),
    ]

    scheduler: Scheduler = Field(default_factory=Scheduler)
    general_config: TicketGeneralConfig = Field(default_factory=TicketGeneralConfig)
    invite_config: InviteConfig = Field(default_factory=InviteConfig)
    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)
    general_battle_config: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)

