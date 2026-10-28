# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import random
from time import sleep
from datetime import time, datetime, timedelta

from module.atom.click import RuleClick
from module.logger import logger
from module.exception import GameStuckError, TaskEnd
from module.base.timer import Timer

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_delegation
from tasks.Delegation.config import DelegationConfig
from tasks.Delegation.assets import DelegationAssets
from toolkit.Lib.logging import Logger


class ScriptTask(GameUi, DelegationAssets):

    def run(self):
        self.goto_page(page_delegation)
        self.check_reward()
        con: DelegationConfig = self.config.delegation.delegation_config
        if con.miyoshino_painting:
            self.delegate_one('画')
        if con.bird_feather:
            self.delegate_one('鸟羽')
        if con.find_earring:
            self.delegate_one('寻找耳环')
        if con.cat_boss:
            self.delegate_one('猫老大')
        if con.miyoshino:
            self.delegate_one('接送')
        if con.strange_trace:
            self.delegate_one('痕迹')


        self.set_next_run(task='Delegation', success=True, finish=True)
        raise TaskEnd

    def delegate_one(self, name: str) -> bool:
        """
        委派一个任务
        :param name:
        :return:
        """
        def ui_click(click, stop):
            while 1:
                self.screenshot()
                if self.appear(stop):
                    break
                if self.click(click, interval=1.5):
                    continue
        logger.hr('Delegation one', 2)
        self.O_D_NAME.keyword = name
        self.screenshot()
        # if not self.ocr_appear(self.O_D_NAME):
        #     logger.warning(f'Delegation: {name} not found')
        #     return False
        # 改：OCR 识别到任务名，或图像 I_TEST 出现，都算找到
        matched_by_ocr = self.ocr_appear(self.O_D_NAME)
        matched_by_image = self.appear(self.I_D_SPECIAL_1)
        if not (matched_by_ocr or matched_by_image):
            logger.warning(f'Delegation: {name} not found')
            return False

        while 1:
            self.screenshot()
            if self.appear(self.I_D_START):
                break
            # 如果出现’召回‘ ’返回‘ 说明这个是现在委派中
            # 需要退出
            if self.appear(self.I_D_BACK):
                logger.warning(f'Delegation: {name} is in delegation')
                self.ui_click_until_disappear(self.I_D_BACK)
                self.wait_until_appear(self.I_REWARDS_MIN)
                return False
            if self.appear_then_click(self.I_D_SKIP, interval=0.8):
                continue
            if self.appear_then_click(self.I_D_CONFIRM, interval=0.8):
                continue
            if self.appear_then_click(self.I_D_SPECIAL_1, interval=1):# 新增：图像 I_D_SPECIAL_1特殊委派 出现就点击
                continue
            if self.ocr_appear_click(self.O_D_NAME, interval=1):
                continue
        # 进入委派页面
        logger.info(f'Enter Delegation: {name}')
        ui_click(self.C_D_1, self.I_D_SELECT_1)
        ui_click(self.C_D_2, self.I_D_SELECT_2)
        ui_click(self.C_D_3, self.I_D_SELECT_3)
        # ui_click(self.C_D_4, self.I_D_SELECT_4)
        # 委派开始
        logger.info(f'Delegation: {name} start')
        while 1:
            self.screenshot()
            if not self.appear(self.I_D_START):
                break
            if self.click(self.C_D_4, interval=0.8):
                    continue
            if self.click(self.C_D_5, interval=0.8):
                continue
            if self.appear_then_click(self.I_D_START, interval=1.8):
                continue
        # ui_click(self.C_D_5, self.I_D_SELECT_5)
        # self.ui_click_until_disappear(self.I_D_START)

    def check_reward(self):
        check_timer = Timer(3)
        check_timer.start()
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_REWARDS_GET, interval=1):
                check_timer.reset()
                continue
            if self.appear_then_click(self.I_REWARDS_CHAT, interval=1):
                check_timer.reset()
                continue
            if self.appear_then_click(self.I_CHAT_1, interval=1):
                check_timer.reset()
                continue
            if self.appear_then_click(self.I_CHAT_2, interval=1):
                check_timer.reset()
                continue
            if self.appear_then_click(self.I_REWARDS_DONE, interval=1):
                check_timer.reset()
                continue
            if self.appear_then_click(self.I_REWARDS_FALSE, interval=1):
                check_timer.reset()
                continue


            if check_timer.reached():
                break
            if not self.appear(self.I_REWARDS_MIN):
                continue
            if self.ocr_appear(self.O_D_DONE):
                x, y, w, h = self.O_D_DONE.area
                # 转成整数，避免浮点坐标
                x, y, w, h = int(x), int(y), int(w), int(h)# 下方同尺寸区域
                _tep_d_done_1 = RuleClick(
                    roi_front=(x, y + h, w, h),
                    roi_back=(x, y + h, w, h),
                    name=' _tep_d_done_1',
                )
                self.ocr_appear_click(self.O_D_DONE,_tep_d_done_1,interval=1)
                self.wait_delegation_done_reward()
                check_timer.reset()
                continue

    def wait_delegation_done_reward(self):
        """领取已完成委派后推进对话，直到奖励确认按钮出现。

        超时 20s：委派奖励对话有多句，每轮（截图+OCR+匹配+点击）约 2s，
        5s 只够推进 3 次；2026-10-20 oas3 事故即 5s 内对话未推完而崩溃。
        """
        wait_timer = Timer(20)
        wait_timer.start()
        while True:
            self.screenshot()
            if self.appear(self.I_REWARDS_DONE):
                return
            if wait_timer.reached():
                raise GameStuckError(
                    'Delegation reward dialog timed out before rewards done appeared'
                )

            chat_text = self.O_D_DONE_CHAT.ocr(self.device.image).strip()
            if chat_text and self.appear(self.I_REWARDS_CHAT):
                self.click(self.O_D_DONE_CHAT, interval=0.8)
                continue


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    #from memory_profiler import profile
    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    # t.delegate_one('弥助的画')
    t.run()


