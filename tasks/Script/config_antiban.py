# This Python file uses the following encoding: utf-8
# @author runhey
from datetime import timedelta
from enum import Enum
from pydantic import BaseModel, Field

from tasks.Component.config_base import Time, TimeDelta


class AntiBanIdleStrategy(str, Enum):
    """
    防风控看守器把任务顶回唤醒时刻（睡眠窗 / 活跃上限强制休息）时，
    这段时间空闲等待采用的策略。默认跟随“任务队列清空”策略，
    显式选择后与该策略解耦，二者互不影响。

    策略值与 `WhenTaskQueueEmpty` 保持一致（同为 "goto_main" 等字符串），
    因此 runtime 侧无需新增映射。
    """
    FOLLOW_QUEUE_EMPTY = 'follow_queue_empty'
    GOTO_MAIN = 'goto_main'
    CLOSE_GAME = 'close_game'
    CLOSE_EMULATOR_OR_GOTO_MAIN = 'close_emulator_or_goto_main'
    CLOSE_EMULATOR_OR_CLOSE_GAME = 'close_emulator_or_close_game'


class AntiBan(BaseModel):
    enable: bool = Field(default=False, description='antiban_enable_help')
    sleep_start: Time = Field(default=Time(hour=2, minute=0, second=0),
                              description='sleep_start_help')
    sleep_end: Time = Field(default=Time(hour=8, minute=0, second=0),
                            description='sleep_end_help')
    daily_active_limit: TimeDelta = Field(default=timedelta(hours=0),
                                          description='daily_active_limit_help')
    long_rest_duration: TimeDelta = Field(default=timedelta(hours=2),
                                          description='long_rest_duration_help')
    antiban_sleep_idle_strategy: AntiBanIdleStrategy = Field(default=AntiBanIdleStrategy.FOLLOW_QUEUE_EMPTY,
                                                      description='antiban_sleep_idle_strategy_help')
