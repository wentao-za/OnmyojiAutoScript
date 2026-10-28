# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import timedelta
from time import sleep
from module.base.timer import Timer

from module.logger import logger
from module.exception import TaskEnd
from tasks.GameUi.default_pages import page_pet, page_shikigami_records
from tasks.Orochi.config import Layer
from tasks.Orochi.page import page_orochi
from tasks.Orochi.script_task import ScriptTask as OrochiScriptTask
from tasks.GameUi.page import page_main
from tasks.Pets.assets import PetsAssets
from tasks.Pets.config import PetsConfig

class ScriptTask(OrochiScriptTask, PetsAssets):

    conf: PetsConfig

    def before_run(self):
        # 宠物后院不在页面注册表内（navigator 识别不了，愁"页面孤儿"）。
        # 把它注册为本任务专属的未知页关闭动作：任何残留后院的会话，
        # 兜底关闭时优先点左下角"小屋"按钮（I_PET_BACKYARD 仅在后院页面命中，
        # 宠物小屋内 MISS，不会误伤）回到可识别的宠物小屋。
        self.navigator.add_unknown_closer(self.I_PET_BACKYARD)

    def run(self):
        self.before_run()
        self.conf = self.config.pets.pets_config
        self.goto_page(page_pet)
        if self.conf.pets_feast:
            self._feed()
            if self.conf.pet_food_card:
                self.goto_page(page_pet)   # 确保 food_card 在 page_pet 上执行
            else:
                # 只启用 pets_feast 时，_feed() 后可能停在 page_pet 或中间页
                # 不额外处理，交给后面的 goto_page(page_main) 兜底
                logger.info('pets_feast done, continue to goto page_main')
        if self.conf.pet_food_card:
            self.food_card()
        self.goto_page(page_main)  # 无条件回主界面
        if self.conf.enable_orochi_ten_once:
            self.run_orochi()
            self.goto_page(page_main)
        self.set_next_run(task='Pets', success=True, finish=True)
        raise TaskEnd('Pets')

    def _feed(self):
        """快速喂养"""
        logger.hr('Feed', 3)
        self.ui_click(self.I_PET_FEAST, self.I_PET_FEED)
        number = self.O_PET_FEED_AP.ocr(self.device.image)
        if number == 0:
            # 已经投喂过了
            logger.warning('Already feed')
            self.appear_then_click(self.I_UI_BACK_CIRCLE)
            return
        self.ui_click(self.I_PET_FEED, self.I_PET_SKIP)
        self.ui_click_until_disappear(self.I_PET_SKIP)

    def run_orochi(self):
        """运行一次御魂十层"""
        logger.hr('Run Orochi', 3)
        self.config.orochi.orochi_config.layer = Layer.TEN
        self.limit_count = 1
        self.limit_time = timedelta(hours=10)
        self.goto_page(page_orochi)
        if self.conf.enable_switch_layer_soul:
            self.switch_orochi_souls()
        self.run_alone()

    def food_card(self):
        """宠物食材卡投喂"""
        logger.hr('food card', 3)

        # 1. 进入后院（先确保在宠物小屋，失败后重试一次）
        if not self.appear(self.I_PET_COURTYARD):
            self.goto_page(page_pet)   # 回宠物小屋
        if not self.ui_click_until_appear_or_timeout(
                self.I_PET_COURTYARD, self.I_PET_BACKYARD, interval=2, timeout=15):
            # 重试一次：先回主界面再重新导航到宠物小屋
            self.goto_page(page_main)
            self.goto_page(page_pet)
            if not self.ui_click_until_appear_or_timeout(
                    self.I_PET_COURTYARD, self.I_PET_BACKYARD, interval=2, timeout=15):
                logger.warning('无法进入宠物后院，终止 food_card')
                return

        # 2. 收取宠物币（前置归位操作，无可收金币也要点）
        self._collect_coins_at(self.C_PET_COLLECT_COINS_DOWN)
        self._collect_coins_at(self.C_PET_COLLECT_COINS_UP)

        # 3. 上方放置食材（失败也继续下方）
        if not self._place_food_at(self.I_PET_UP_FOOD, '上方'):
            logger.warning('上方食材未成功放置，继续尝试下方')

        # 4. 下方放置食材
        if not self._place_food_at(self.I_PET_DOWN_FOOD, '下方'):
            logger.warning('下方食材未成功放置')

        logger.info('food card 完成')
        # 退出后院：点左下"小屋"回宠物小屋（I_PET_BACKYARD 仅后院页面命中），
        # 再由后续 goto_page(page_main) 走 page_pet 的正常回退链路。
        # 直接点 BACK_CIRCLE 也能退出，但若停留在不可识别的后院页面，
        # navigator 无法认领页面会卡进死循环（oas3 11:16 崩溃根因）。
        # 点击直到回到宠物小屋（"前往后院"按钮重新出现）
        if not self.ui_click_until_appear_or_timeout(
                self.I_PET_BACKYARD, self.I_PET_COURTYARD, interval=2, timeout=10):
            logger.warning('宠物后院退出按钮未命中，交给兜底导航')
        self.ui_click_until_disappear(self.I_UI_BACK_CIRCLE)

    def _collect_coins_at(self, coin_click):
        """点击一处宠物币；若出现确认弹窗，点空白处关闭并等待弹窗消失。

        用等待弹窗出现代替固定 sleep：设备/网络慢时弹窗可能晚出现，
        固定 sleep 会漏判，导致弹窗残留遮挡后续点击。
        """
        self.click(coin_click, interval=1)
        if not self.wait_until_appear(self.I_PET_COLLECT_COINS_CHECK, wait_time=3):
            logger.info(f'{coin_click.name} 点击后无确认弹窗（可能无可收金币），继续')
            return
        # 逐个重试点空白处关闭弹窗，直到弹窗消失（最多 3 秒）
        timer = Timer(3).start()
        while not timer.reached():
            self.click(self.C_PET_SAFE_CLICK_AREA)
            self.screenshot()
            if not self.appear(self.I_PET_COLLECT_COINS_CHECK):
                break
            sleep(0.3)

    def _place_food_at(self, position_icon, position_name: str) -> bool:
        """在指定位置放置食材。

        食材可能不足：点击 C_PET_FOOD_LAY 后若无可放食材，不会出现摆放
        正确标志。此时最多重试 3 次，仍失败则记录日志并返回 False，
        由调用方继续后续流程，不在此处卡住。
        """
        # 1. 打开食材面板
        if not self.ui_click_until_appear_or_timeout(position_icon, self.I_PET_FOOD, interval=2, timeout=20):
            logger.warning(f'[{position_name}] 无法打开食材面板，跳过该位置')
            return False

        # 2. 点击食材并确认摆放正确，最多 3 次
        placed = False
        for attempt in range(3):
            self.click(self.C_PET_FOOD_LAY, interval=1)
            if self._wait_food_placed(timeout=3.0):
                placed = True
                break
            logger.warning(
                f'[{position_name}] 第 {attempt + 1}/3 次点击食材后，'
                f'未检测到猫粮或狗粮摆放正确标志（可能食材不足）'
            )

        # 3. 无论成功与否，都尝试点击确认按钮清理界面状态
        #    成功时关闭正常确认弹窗；失败时若游戏弹出了“食材不足”类提示
        #    且带确认按钮，也一并关掉，避免遮挡后续下方放置操作。
        self.ui_click_until_disappear(self.I_PET_FOOD_CHECK)

        if not placed:
            logger.warning(
                f'[{position_name}] 食材未成功放置（已重试 3 次，可能猫粮/狗粮不足），'
                f'跳过该位置继续后续流程'
            )
            self.ui_click_until_disappear(self.I_UI_BACK_RED)
            return False

        logger.info(f'[{position_name}] 食材放置成功')
        return True

    def _wait_food_placed(self, timeout: float = 5.0) -> bool:
        """等待食材摆放正确的标志出现（猫粮或狗粮任一）。

        I_PET_CAT_FOOD_CHECK / I_PET_DOG_FOOD_CHECK 分别是猫粮/狗粮摆放
        正确的视觉标志，不可点击。OAS 无 wait_until_appear_any，这里用
        screenshot + 两次独立 appear 手动实现“任一出现即返回”。
        Timer 保证有超时，不会无限等待。
        """
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            if self.appear(self.I_PET_CAT_FOOD_CHECK) or self.appear(self.I_PET_DOG_FOOD_CHECK):
                return True
            sleep(0.3)
        return False


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()

    t.run()
