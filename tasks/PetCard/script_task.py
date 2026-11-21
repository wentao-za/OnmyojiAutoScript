# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep

from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_pet
from tasks.PetCard.config import PetCardConfig
from tasks.Pets.assets import PetsAssets


class ScriptTask(GameUi, PetsAssets):
    """宠物挂卡：进入宠物后院，收取宠物币并放置食材卡。

    素材直接复用 PetsAssets（tasks/Pets/pet 下的 pet_* 规则），
    与 tasks/Pets 的 food_card 共用同一份 ROI，不另存 png/json。
    """

    conf: PetCardConfig

    def before_run(self):
        # 宠物后院不在页面注册表内（navigator 识别不了，属"页面孤儿"）。
        # 把它注册为本任务专属的未知页关闭动作：任何残留后院的会话，
        # 兜底关闭时点左下角"小屋"按钮（I_PET_BACKYARD 仅在后院页面命中，
        # 宠物小屋内 MISS，不会误伤）回到可识别的宠物小屋。
        self.navigator.add_unknown_closer(self.I_PET_BACKYARD)

    def run(self):
        self.before_run()
        self.conf = self.config.pet_card.pet_card_config
        self.goto_page(page_pet)
        self.food_card()
        self.goto_page(page_main)  # 无条件回主界面
        self.set_next_run(task='PetCard', success=True, finish=True)
        raise TaskEnd('PetCard')

    def food_card(self):
        """宠物食材卡投喂"""
        # level 3 细分阶段标题：按 OAS_development.md §3.6 用中文即可
        # （仅 level 0/1 需中英文对照）。
        logger.hr('宠物挂卡', 3)

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
                logger.warning('无法进入宠物后院，终止宠物挂卡')
                return

        # 2. 收取宠物币（前置归位操作，无可收金币也要点）
        if self.conf.collect_coins:
            self._collect_coins_at(self.C_PET_COLLECT_COINS_DOWN)
            self._collect_coins_at(self.C_PET_COLLECT_COINS_UP)

        # 3. 上方放置食材（失败也继续下方）
        if self.conf.place_food:
            if not self._place_food_at(self.I_PET_UP_FOOD, '上方'):
                logger.warning('上方食材未成功放置，继续尝试下方')

            # 4. 下方放置食材
            if not self._place_food_at(self.I_PET_DOWN_FOOD, '下方'):
                logger.warning('下方食材未成功放置')

        logger.info('宠物挂卡完成')
        # 退出后院：点左下"小屋"回宠物小屋（I_PET_BACKYARD 仅后院页面命中），
        # 再由后续 goto_page(page_main) 走 page_pet 的正常回退链路。
        # 直接点 BACK_CIRCLE 也能退出，但若停留在不可识别的后院页面，
        # navigator 无法认领页面会卡进死循环。
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
            logger.info(f'{coin_click.name} 点击后未匹配到收币弹窗图标（可能无可收金币），继续')
            return
        # 逐个重试点空白处关闭弹窗，直到弹窗消失（最多 3 秒）
        timer = Timer(3).start()
        while not timer.reached():
            self.click(self.C_PET_SAFE_CLICK_AREA)
            self.screenshot()
            if not self.appear(self.I_PET_COLLECT_COINS_CHECK):
                break
            sleep(0.3)

    def _dismiss_leftover_popup(self):
        """点一次后院空白处，清掉可能残留的收币弹窗。

        收币弹窗没有稳定可用的图像判据时（模板失配、阈值未达），无法确认它
        是否在场；C_PET_SAFE_CLICK_AREA 落在后院空地，弹窗不在场时点击无副作用。
        只点一次：点击记录里同名按钮累计 10 次会抛 GameTooManyClickError
        （OAS_pitfalls.md 坑位 60），这里每次进入食材面板最多消耗 1 次额度。
        """
        self.click(self.C_PET_SAFE_CLICK_AREA, interval=1)

    def _place_food_at(self, position_icon, position_name: str) -> bool:
        """在指定位置放置食材。

        食材可能不足：点击 C_PET_FOOD_LAY 后若无可放食材，不会出现摆放
        正确标志。此时最多重试 3 次，仍失败则记录日志并返回 False，
        由调用方继续后续流程，不在此处卡住。
        """
        # 1. 打开食材面板。
        #    收币弹窗（"亲密度已满"）带全屏暗化蒙版，会把食材入口压暗到匹配分
        #    不足：2026-11-20 08:41 实测弹窗在场时 I_PET_UP_FOOD 在 roi_back 内
        #    只有 0.7356（阈值 0.8），该 ROI 像素均值 BGR≈(13.9,13.4,14.5)。
        #    收币判据一旦失配（I_PET_COLLECT_COINS_CHECK 未命中），弹窗就没人
        #    关闭并一直盖在上面，该位置必然打不开。所以先清一次残留弹窗再重试。
        if not self.ui_click_until_appear_or_timeout(position_icon, self.I_PET_FOOD, interval=2, timeout=20):
            logger.warning(f'[{position_name}] 无法打开食材面板，清除残留弹窗后重试')
            self._dismiss_leftover_popup()
            if not self.ui_click_until_appear_or_timeout(
                    position_icon, self.I_PET_FOOD, interval=2, timeout=10):
                logger.warning(f'[{position_name}] 清除残留弹窗后仍无法打开食材面板，跳过该位置')
                return False

        # 2. 点击食材并确认摆放正确，最多 3 次。
        #    观察窗用 5s 而不是 3s：日志里反复出现"第 1 次点击 3s 内无标志、
        #    紧接着一次点击 1.4s 就出标志"（11-11 11:13 间隔 1.379s、
        #    11-10 02:31 间隔 1.674s、11-10 09:52 间隔 1.369s），说明 3s
        #    会把"首次点击被客户端吞掉"误判成食材不足，白烧一次点击额度。
        placed = False
        for attempt in range(3):
            self.click(self.C_PET_FOOD_LAY, interval=1)
            if self._wait_food_placed(timeout=5.0):
                placed = True
                break
            logger.warning(
                f'[{position_name}] 第 {attempt + 1}/3 次点击食材后，'
                f'未检测到猫粮或狗粮摆放正确标志（可能食材不足）'
            )

        # 3. 收尾：把"放上了"和"提交成功"分开判。
        #    有卡 = 面板里出现了摆放标志，但卡要等右下"确认"提交给服务器、
        #    且确认把面板关掉才算真放上；无卡 = 点击放置无任何反馈，只能
        #    红叉退出。红叉只负责退出面板，任何路径都不得拿它当成功证据。
        if not placed:
            if not self._close_food_panel_by_back():
                logger.warning(f'[{position_name}] 食材面板未能关闭，后续点击可能被遮挡')
            logger.warning(
                f'[{position_name}] 食材未成功放置（已重试 3 次，可能猫粮/狗粮不足），'
                f'跳过该位置继续后续流程'
            )
            return False

        if self._confirm_food_panel():
            logger.info(f'[{position_name}] 食材放置成功')
            return True

        # 确认没把面板关掉 = 卡没提交。此时面板可能还开着，红叉退出；
        # 即使红叉点掉了面板，也只是关闭 UI，不代表放置成功。
        logger.warning(
            f'[{position_name}] 确认按钮未生效（面板未关闭），本次放置未完成，'
            f'红叉退出食材面板'
        )
        self._close_food_panel_by_back()
        return False

    def _wait_panel_closed(self, timeout: float = 6.0) -> bool:
        """只观察不点击，等待食材面板消失。

        与 `ui_click_until_disappear` 的关键差别：那个函数在 `max_clicks`
        用尽后**立刻**返回，不给"点击已生效但画面未刷新"留时间；这里纯
        轮询，专门用来给单次点击一个完整的响应窗口。
        """
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            if not self.appear(self.I_PET_FOOD):
                return True
            sleep(0.5)
        return False

    def _confirm_food_panel(self) -> bool:
        """点右下"确认"提交食材卡，返回面板是否因确认而关闭。

        节奏是"点一次 → 等满 6s → 仍不关才再点一次"，不是连击。原因：
        确认按钮与放置按钮同源，都存在首次点击被客户端吞掉、或提交后
        画面 3~4s 才刷新的情况（放置按钮的日志证据见 `_place_food_at`）。
        `ui_click_until_disappear` 用 `interval=1` 连点，3 次点击会全落在
        未响应窗口内，等于只点了一次，还把点击额度烧光——`click_record`
        同名按钮 ≥10 次、两个按钮各 ≥6 次都会抛 `GameTooManyClickError`
        （见 OAS_pitfalls.md 坑位 60）。

        所以这里次数压到 2、等待拉到 6s+3s：跨上下两个位置确认按钮最多
        4 次，远离硬限；每次点击都有完整响应窗口，能区分"被吞"和"确实
        提交不上去"。
        """
        for attempt in (1, 2):
            if not self.appear_then_click(self.I_PET_FOOD_CHECK, interval=1):
                logger.warning('确认按钮未命中（面板上找不到确认按钮规则）')
                return False
            if self._wait_panel_closed(timeout=6):
                return True
            logger.info(f'确认第 {attempt}/2 次点击后 6s 内面板未关闭')

        # 宽限：提交请求可能仍在路上，再纯等一次，不追加点击。
        return self._wait_panel_closed(timeout=3)

    def _close_food_panel_by_back(self) -> bool:
        """用右上角红叉关闭食材面板，返回面板是否已消失。

        只负责"退出面板"，不构成任何提交成功的证据——调用方不得据此判定
        放置成功（坑位 60：旧版拿它当成功，卡没提交却打印"放置成功"）。
        判据必须用 `I_PET_FOOD`（面板标题）消失，不能用被点的红叉自身：
        无卡时按钮恒在，拿它当判据会一直点下去撞 10 次硬限（坑位 59）。
        """
        self.ui_click_until_disappear(
            self.I_UI_BACK_RED, interval=1,
            visible=lambda: self.appear(self.I_PET_FOOD),
            timeout=5, max_clicks=2, confirm_frames=2,
        )
        self.screenshot()
        return not self.appear(self.I_PET_FOOD)

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
