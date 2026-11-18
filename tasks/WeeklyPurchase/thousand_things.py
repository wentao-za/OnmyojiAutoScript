# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

from module.logger import logger
from module.base.timer import Timer

from tasks.GameUi.page import page_main, page_travel
from tasks.GameUi.game_ui import GameUi
from tasks.WeeklyPurchase.assets import WeeklyPurchaseAssets
from tasks.WeeklyPurchase.config import ThousandThings as ConfigThousandThings

class ThousandThings(GameUi, WeeklyPurchaseAssets):

    def execute_tt(self, con: ConfigThousandThings) -> None:
        """

        :param con:
        :return:
        """
        logger.hr('千事')
        if not con.enable:
            logger.info('千事未启用')
            return
        self.goto_page(page_travel)

        if con.earn_money:
            logger.info('开始借式神赚金币')
            self.tt_earn_money()

        while 1:
            self.screenshot()
            if self.appear(self.I_TT_CHECK):
                break
            if self.appear_then_click(self.I_TT_ENTER, interval=1):
                continue
        logger.info('进入千事')
        self.screenshot()
        if not self.appear(self.I_TT_TICKET_BULE) and not self.appear(self.I_TT_BLACK) and not self.appear(self.I_TT_AP):
            time.sleep(1)
        if con.mystery_amulet:
            self.tt_buy_mystery_amulet()
        self.device.click_record_clear()
        if con.black_daruma_fragment:
            self.tt_buy_black_daruma_scrap()
        self.device.click_record_clear()
        if con.ap:
            self.tt_buy_ap()
        self.device.click_record_clear()
        while 1:
            self.screenshot()
            if self.appear(self.I_TT_ENTER):
                break
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue
        logger.info('退出千事')

    def tt_buy_mystery_amulet(self) -> bool:
        """

        :return: 成功购买返回True，找不到或者是钱不够返回False
        """
        self.screenshot()
        if not self.appear(self.I_TT_TICKET_BULE):
            logger.info('没有蓝票')
            return False
        if not self.tt_check_money(2000):
            return False
        while 1:
            self.screenshot()
            if self.appear(self.I_TT_BUY_UP):
                break
            if self.ocr_appear_click(self.O_TT_BLUE_TICKET, interval=1):
                continue
        logger.info('购买蓝票')
        if not self.tt_get_reward(self.I_TT_BUY_CONFIRM):
            return False
        logger.info('蓝票购买成功')
        time.sleep(0.5)
        return True

    def tt_buy_black_daruma_scrap(self):
        self.screenshot()
        if not self.appear(self.I_TT_BLACK):
            logger.info('没有黑达摩碎片')
            return False
        if not self.tt_check_money(350):
            return False
        while 1:
            self.screenshot()
            if self.appear(self.I_TT_BUY_UP):
                break
            if self.ocr_appear_click(self.O_TT_BLACK, interval=1):
                continue
        logger.info('购买黑达摩碎片')
        if not self.tt_get_reward(self.I_TT_BUY_CONFIRM):
            return False
        logger.info('黑达摩碎片购买成功')
        time.sleep(0.5)
        return True

    def tt_buy_ap(self):
        self.screenshot()
        if not self.appear(self.I_TT_AP):
            logger.info('没有体力礼盒')
            return False
        if not self.tt_check_money(600):
            return False
        # ① 点开体力礼盒的购买面板：一直点价格，直到面板内的按钮出现，超时即认输
        #    数量拉满键(双箭头)与购买确认键只在购买面板里出现，二者任一看得见就等于面板已开
        open_timer = Timer(15).start()
        while 1:
            self.screenshot()
            if self.appear(self.I_TT_BUY_UP) or self.appear(self.I_TT_BUY_CONFIRM):
                break
            if open_timer.reached():
                logger.warning('购买体力失败：购买面板未打开(buy ap failed: panel not open)')
                return False
            self.ocr_appear_click(self.O_TT_AP, interval=1.5)
        # ② 点一次「数量拉满」把数量顶到限购上限，然后直接买，不再用 OCR 校验数量
        if self.appear_then_click(self.I_TT_BUY_UP, interval=0.5):
            time.sleep(0.5)
        logger.info('购买体力礼盒')
        if not self.tt_get_reward(self.I_TT_BUY_CONFIRM):
            return False
        logger.info('体力礼盒购买成功')
        time.sleep(0.5)
        return True

    def tt_check_money(self, mix: int) -> bool:
        self.screenshot()
        current = self.O_S_TOTAL.ocr(self.device.image)
        if not isinstance(current, int):
            logger.warning('OCR 当前货币失败')
            return False
        if current >= mix:
            logger.info('货币充足')
            return True
        logger.info('货币不足')
        return False

    def tt_get_reward(self, image_button) -> bool:
        """点购买确认键并领奖。True=已进入领奖流程，False=没点成（面板没开或按钮失配）"""
        from time import sleep
        click_count = 0
        # 判死线是 device 的 60 秒零点击，这里必须远早于它退出，否则整条流程被踢下看门狗
        timeout_timer = Timer(20).start()
        while 1:
            self.screenshot()

            if click_count >= 6 or timeout_timer.reached():
                logger.warning('领取奖励失败')
                close_timer = Timer(10).start()
                while not close_timer.reached():
                    self.screenshot()
                    if not self.appear(self.I_TT_BUY_UP):
                        break
                    if self.appear_then_click(self.I_UI_BACK_RED, interval=3):
                        continue
                return False

            if self.ui_reward_appear_click():
                sleep(0.5)
                reward_timer = Timer(15).start()
                while 1:
                    self.screenshot()
                    # 等待动画结束
                    if not self.appear(self.I_UI_REWARD, threshold=0.6):
                        logger.info('领取奖励成功')
                        break
                    if reward_timer.reached():
                        logger.warning('奖励页 15 秒未消失，先退出')
                        break

                    # 一直点击
                    if self.ui_reward_appear_click():
                        continue
                return True

            if self.appear_then_click(image_button, interval=2):
                click_count += 1
                continue
        return False

    def tt_earn_money(self):

        # 进入界面
        self.ui_click(self.I_TT_BORROW, self.I_TT_CONFIGURE)
        self.ui_click(self.I_TT_CONFIGURE, self.I_TT_CONFIRM)
        #替换式神
        self.ui_click(self.I_TT_SHIKIGAMI, self.I_TT_SHIKIGAMI_REPLACE)
        while 1:
            self.screenshot()
            if not self.appear(self.I_TT_SHIKIGAMI_REPLACE):
                break
            if self.appear_then_click(self.I_TT_SHIKIGAMI_REPLACE):
                continue
            if self.appear_then_click(self.I_UI_CONFIRM):
                continue
        # ① 先关"是否更换"确认弹窗（弹窗页面 TT_TT_CONFIRM 也命中，必须先关弹窗）
        for _ in range(5):
            self.screenshot()
            if not self.appear(self.I_UI_CONFIRM):
                break
            self.click(self.I_UI_CONFIRM, interval=1)

        # ② 点右下"确认"直到借出留言页出现
        timeout_timer = Timer(30).start()
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_TT_BORROW_CONFIRM):
                break
            self.appear_then_click(self.I_TT_CONFIRM, interval=1)

        # 获取奖励
        self.ui_get_reward(self.I_TT_BORROW_CONFIRM)

        # 退出界面
        self.ui_click(self.I_UI_BACK_RED, self.I_TT_ENTER)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    t = ThousandThings(c, d)

    t.execute_tt(t.config.weekly_purchase.thousand_things)





