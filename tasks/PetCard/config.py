# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import Field

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase


class PetCardConfig(ConfigBase):
    # 收取宠物币（后院内上下两处）
    collect_coins: bool = Field(default=True, description='pet_card_collect_coins_help')
    # 放置食材卡（后院内上方、下方两个槽位）
    place_food: bool = Field(default=True, description='pet_card_place_food_help')


class PetCard(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    pet_card_config: PetCardConfig = Field(default_factory=PetCardConfig)
