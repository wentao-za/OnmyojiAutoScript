# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
import random

from random import randint

from tasks.Component.GeneralRoom.assets import GeneralRoomAssets
from module.atom.ocr import RuleOcr
from module.atom.image import RuleImage
from tasks.base_task import BaseTask
from module.logger import logger
from module.base.timer import Timer


class GeneralRoom(BaseTask, GeneralRoomAssets):

    def create_room(self, create_room_rule: RuleImage = None, ensure_rules: list[RuleImage] = None) -> bool:
        """
        创建队伍  一般是下方的黄色按钮
        :param create_room_rule: 创建房间入口按钮；缺省用组件自带资产
        :param ensure_rules: 创建确认弹窗的识别规则；缺省用组件自带资产
        :return:
        """
        logger.info('Create room')
        create_room_rule = self.I_CREATE_ROOM if create_room_rule is None else create_room_rule
        ensure_rules = ensure_rules if ensure_rules is not None else [self.I_CREATE_ENSURE, self.I_CREATE_ENSURE_2]
        if not self.appear(create_room_rule):
            logger.warning('No create room button')
            return False
        click_number = 0
        while 1:
            self.screenshot()
            if click_number > 3:
                logger.warning('Create room button do not take effect')
                logger.warning(
                    'The most possible reason is that there are not challenge tickets'
                )
                return False
            if self.appear_then_click(create_room_rule, interval=2):
                click_number += 1
                continue
            if any(self.appear(rule) for rule in ensure_rules):
                return True
        return False

    def ensure_private(self, room_mark: RuleImage = None,
                       private_rules: list[RuleImage] = None,
                       private_false_rules: list[RuleImage] = None) -> bool:
        """
        确认私人房间, 不公开仅邀请
        :param room_mark: 已进入房间的标志；用于处理建房弹窗关闭时的过渡帧
        :param private_rules: 已勾选「不公开」的识别规则；缺省用组件自带资产
        :param private_false_rules: 未勾选「不公开」的识别规则；缺省用组件自带资产
        :return:
        """
        logger.info('Ensure private')
        checked = private_rules if private_rules is not None else [self.I_ENSURE_PRIVATE, self.I_ENSURE_PRIVATE_2]
        unchecked = private_false_rules if private_false_rules is not None else \
            [self.I_ENSURE_PRIVATE_FALSE, self.I_ENSURE_PRIVATE_FALSE_2]
        timeout = Timer(15).start()
        while 1:
            self.screenshot()
            if timeout.reached():
                logger.warning('Ensure private timeout')
                return False
            if any(self.appear(rule) for rule in checked):
                return True
            if any(self.appear_then_click(rule, interval=1) for rule in unchecked):
                continue
            if self.appear_then_click(self.I_ENSURE_PRIVATE_FALSE_2, interval=1):
                continue
            if self.appear_then_click(self.I_ENSURE_PRIVATE_FALSE_3, interval=1):
                continue
            # 弹窗上的开关匹配不到但房间已经建成: 私人设置此前已生效(或沿用上次设置), 直接放行
            if room_mark is not None and self.appear(room_mark):
                logger.info('Room already created, private ensured')
                return True
        return False

    def ensure_public(self, room_mark: RuleImage = None,
                      public_rules: list[RuleImage] = None,
                      public_false_rules: list[RuleImage] = None) -> bool:
        """确认公开房间
        :param room_mark: 已进入房间的标志；用于处理建房弹窗关闭时的过渡帧
        :param public_rules: 已勾选「公开」的识别规则；缺省用组件自带资产
        :param public_false_rules: 未勾选「公开」的识别规则；缺省用组件自带资产
        :return:
        """
        logger.info('Ensure public')
        checked = public_rules if public_rules is not None else [self.I_ENSURE_PUBLIC, self.I_ENSURE_PUBLIC_2]
        unchecked = public_false_rules if public_false_rules is not None else \
            [self.I_ENSURE_PUBLIC_FALSE, self.I_ENSURE_PUBLIC_FALSE_2]
        timeout = Timer(15).start()
        while 1:
            self.screenshot()
            if timeout.reached():
                logger.warning('Ensure public timeout')
                return False
            if any(self.appear(rule) for rule in checked):
                return True
            if any(self.appear_then_click(rule, interval=1) for rule in unchecked):
                continue
            if room_mark is not None and self.appear(room_mark):
                logger.info('Room already created, public ensured')
                return True
        return False

    def create_ensure(self, ensure_rules: list[RuleImage] = None) -> bool:
        """
        创建确认
        :param ensure_rules: 确认按钮的识别规则；缺省用组件自带资产
        :return:
        """
        logger.info('Create ensure')
        rules = ensure_rules if ensure_rules is not None else [self.I_CREATE_ENSURE, self.I_CREATE_ENSURE_2]
        target = None
        for rule in rules:
            if rule.match(self.device.image, frame_id=self.device.image_frame_id):
                target = rule
                break
        if not target:
            logger.warning('No create ensure button')
            return False

        while True:
            self.screenshot()
            if self.appear_then_click(target, interval=1.5):
                continue
            if not self.appear(target):
                return True
        return False

    def exit_team(self) -> bool:
        """
        在组队界面 退出组队的界面， 返回到庭院或者是你一开始进入的入口
        :return:
        """
        if self.appear(self.I_CHECK_TEAM):
            logger.info('Exit team ui')
            while 1:
                self.screenshot()
                if not self.appear(self.I_CHECK_TEAM):
                    return True
                if self.appear_then_click(self.I_GR_BACK_YELLOW, interval=0.5):
                    continue
        return False

    def check_zones(self, *names: str) -> bool:
        """
        确认副本的名称，并选中。
        支持传入多个候选关键字，任意一个在列表中匹配即进入。
        例如：check_zones('愤怒的石距', '喷怒的石距')
        :param names: 一个或多个候选关键字
        :return: 是否成功进入
        """
        #没有传入任何关键字时直接返回
        if not names:
            return False

        #循环尝试每个候选关键字，任意一个匹配即可
        pos = None
        matched_name = None
        for name in names:
            pos = self.list_find(self.L_TEAM_LIST, name)
            if pos:
                matched_name = name
                break

        if not pos:
            return False

        #用元组统一管理“愤怒的石距”系列的所有 OCR 变体
        #替换原来的 if name == '愤怒的石距' or name == '喷怒的石距':
        FURIOUS_ZONE_NAMES = ('愤怒的石距', '喷怒的石距', '愤恕的石距', '喷恕的石距')
        #拆分出 ocr_keyword，避免与列表关键字 matched_name 混用
        if matched_name in FURIOUS_ZONE_NAMES:
            ocr_keyword = '价悠的石距'
        else:
            ocr_keyword = matched_name

        #使用 ocr_keyword 而非 name
        self.O_GR_ZONES_NAME.keyword = ocr_keyword

        click_timer = Timer(1.1)
        click_timer.start()
        while 1:
            self.screenshot()

            if self.ocr_appear(self.O_GR_ZONES_NAME):
                break

            # https://github.com/runhey/OnmyojiAutoScript/issues/488
            # 只能说朴实无华
            text_ocr = self.O_GR_ZONES_NAME.ocr(self.device.image)
            if matched_name == '石距' and '石距' in text_ocr:
                break
            if matched_name == '金币妖怪' and "金币" in text_ocr:
                break
            if matched_name == '经验妖怪' and '经验' in text_ocr:
                break
            if matched_name in FURIOUS_ZONE_NAMES and '石距' in text_ocr:
                break
            if click_timer.reached():
                click_timer.reset()
                self.device.click(x=pos[0] + randint(-5, 5), y=pos[1] + randint(-5, 5))

        return True
