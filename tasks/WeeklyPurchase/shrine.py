# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

from module.logger import logger

from tasks.GameUi.page import page_main, page_summon
from tasks.GameUi.game_ui import GameUi
from tasks.WeeklyPurchase.assets import WeeklyPurchaseAssets
from tasks.WeeklyPurchase.config import Shrine as ConfigShrine


class Shrine(GameUi, WeeklyPurchaseAssets):

    def execute_shrine(self, con: ConfigShrine):
        logger.hr('神龛商店')
        if not con.enable:
            logger.info('神龛商店未启用')
            return
        self.goto_page(page_summon)

        while 1:
            self.screenshot()
            if self.appear(self.I_S_NEXT_PERIOD):
                break
            if self.appear_then_click(self.I_S_SUMMON_TO_SHRINE, interval=2):
                continue
        logger.info('进入神龛商店')
        time.sleep(0.5)
        if con.black_daruma:
            self.shrine_black_daruma()
        if con.white_daruma_five:
            self.shrine_white_five()
        if con.white_daruma_four:
            self.shrine_white_four()

    def shrine_check_money(self, mix: int) -> bool:
        self.screenshot()
        current = self.O_TT_TOTOL.ocr(self.device.image)
        if not isinstance(current, int):
            logger.warning('OCR 当前货币失败')
            return False
        if current >= mix:
            logger.info('货币充足')
            return True
        logger.info('货币不足')
        return False

    def _check_bought(self, target) -> bool:
        """
        检查是否已经购买, 弃用，无法识别任何文字
        :return: True 已经购买
        """
        self.screenshot()
        result = target.ocr(self.device.image)
        if '已' in result or '兑' in result or '换' in result:
            logger.info('已购买')
            return True
        logger.info('未购买')
        return False

    def shrine_black_daruma(self):
        logger.hr('神龛 黑达摩', 2)
        self.screenshot()
        if not self.shrine_check_money(1500):
            return
        if not self.appear(self.I_S_BLACK):
            logger.info('黑达摩已购买')
            return
        self.ui_click(self.I_S_BLACK, self.I_S_CHECK_BLACK)
        self.screenshot()
        if not self.appear(self.I_S_BUY_BLACK, threshold=0.6):
            logger.info('黑达摩已购买')
            self.ui_click_until_disappear(self.I_UI_BACK_RED)
            time.sleep(0.5)
            return
        self.ui_click(self.I_S_BUY_BLACK, self.I_S_CONFIRM_BLACK)
        self.ui_get_reward(self.I_S_CONFIRM_BLACK)
        self.ui_click_until_disappear(self.I_UI_BACK_RED)
        time.sleep(1)

    def shrine_white_five(self):
        logger.hr('神龛 五星白达摩', 2)
        self.screenshot()
        if not self.appear(self.I_S_WHITE_FIVE):
            logger.info('五星白达摩不可购买')
            return
        if not self.shrine_check_money(1200):
            return
        self.ui_click(self.I_S_WHITE_FIVE, self.I_S_CHECK_WHITE_FIVE)
        self.screenshot()
        if not self.appear(self.I_S_BUY_WHITE_FIVE, threshold=0.9):
            logger.info('五星白达摩已购买')
            self.ui_click_until_disappear(self.I_UI_BACK_RED)
            time.sleep(1)
            return
        self.ui_click(self.I_S_BUY_WHITE_FIVE, self.I_S_CONFIRM_WHITE_FIVE)
        self.ui_get_reward(self.I_S_CONFIRM_WHITE_FIVE)
        self.ui_click_until_disappear(self.I_UI_BACK_RED)
        time.sleep(1)

    def shrine_white_four(self):
        logger.hr('神龛 四星白达摩', 2)
        self.screenshot()
        if not self.appear(self.I_S_WHITE_FOUR):
            logger.info('四星白达摩不可购买')
            return
        if not self.shrine_check_money(400):
            return
        self.ui_click(self.I_S_WHITE_FOUR, self.I_S_CHECK_WHITE_FOUR)
        self.screenshot()
        if not self.appear(self.I_S_BUY_WHITE_FOUR, threshold=0.9):
            logger.info('四星白达摩已购买')
            self.ui_click_until_disappear(self.I_UI_BACK_RED)
            time.sleep(1)
            return
        self.ui_click(self.I_S_BUY_WHITE_FOUR, self.I_S_CONFIRM_WHITE_FOUR)
        self.ui_get_reward(self.I_S_CONFIRM_WHITE_FOUR)
        self.ui_click_until_disappear(self.I_UI_BACK_RED)
        time.sleep(1)



if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = Shrine(c, d)

    # t.shrine_white_four()
    t.execute_shrine(t.config.model.weekly_purchase.shrine)
    # t.screenshot()
    # print(t.appear(t.I_S_BUY_WHITE_FIVE, threshold=0.9))


