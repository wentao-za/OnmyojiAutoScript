# This Python file uses the following encoding: utf-8
"""DailyTrifles 公会祈愿「按昵称搜索」的桌面客户端实现。

安卓链路是：点搜索框 → `adb.send_keys` 输入 → 等输入法弹出的「确定」按钮
(`I_DT_GW_CONFIRM`)。桌面客户端不弹输入法、也不走 adb，所以那条链路在桌面上
永远等不到「确定」，连点 10 次后触发防连点保护，任务被判卡死。

桌面链路（本文件）：清空搜索框 → 点输入框 → 逐字发 `WM_CHAR` → 点界面内的
「搜索」按钮 (`I_DT_GW_SEARCH_BTN`) → 等列表刷新。
`WM_CHAR` 按窗口句柄定向投递，客户端不必在前台，双开也不会串键；中文按
Unicode 逐字发，不需要任何输入法，也不会影响本机前台。

只在 `self.device.is_desktop` 为真时被 `script_task.donate()` 调用，
安卓链路的四行代码原样保留，一行不受影响。
"""
from time import sleep

import numpy as np

from module.base.timer import Timer
from module.logger import logger


class DesktopGuildSearch:
    """桌面客户端的「按昵称搜索」分支（mixin，由 ScriptTask 继承）。"""

    # 搜索框内「墨色像素」判读：三通道之和小于它算一个墨点（底色约 600+）
    GW_INK_LUMA_TOTAL = 330
    # 打字后墨点至少增加这么多，才认为文字真的上屏（空框实测 0，四个汉字约 238）
    GW_INK_MIN_DELTA = 60
    # 点「搜索」后等列表刷新的上限（实测 1 秒内刷新，留一半余量）
    GW_SEARCH_WAIT = 1.5
    # 逐字投递的间隔，太快客户端会漏字符
    GW_TYPE_INTERVAL = 0.05

    def guild_donate_search(self, name: str) -> bool:
        """把 name 送进搜索框并触发搜索。

        :param name: 待搜索的昵称，中文与 ASCII 同样走 Unicode 直发
        :return: 文字是否确认上屏且「搜索」按钮已点击；结果判定留给
                 `find_target_name` / `process_donate`，这里不重复判断
        """
        # 清掉上一次的关键词；框内无字时 ✕ 不出现，第一次就返回 False
        for _ in range(2):
            if not self.appear_then_click(self.I_DT_GW_CLEAR_SEARCH):
                break
        ink_before = self.gw_input_ink()  # 空框基线，此时没有光标闪烁干扰

        # 点输入框并注入文字。实测点一次偶发不生效，多点不会改变状态，故点两次
        typed = False
        for _ in range(2):
            self.click(self.C_DT_GW_INPUT_SEARCH)
            self.click(self.C_DT_GW_INPUT_SEARCH)
            if not self.gw_post_text(name):
                logger.warning('桌面客户端：窗口句柄为空，无法输入文字')
                return False
            sleep(0.15)  # 等客户端把最后一帧画出来再判读
            if self.gw_input_ink() - ink_before >= self.GW_INK_MIN_DELTA:
                typed = True
                break
            logger.warning('桌面客户端：搜索框内未出现文字，重试一次')
            self.appear_then_click(self.I_DT_GW_CLEAR_SEARCH)  # 防止文字叠加
        if not typed:
            logger.warning(f'桌面客户端：输入「{name}」失败，跳过本次搜索')
            return False

        if not self.appear(self.I_DT_GW_SEARCH_BTN):
            logger.warning('桌面客户端：未找到「搜索」按钮，跳过本次搜索')
            return False
        self.click(self.I_DT_GW_SEARCH_BTN)
        logger.info(f'桌面客户端：已搜索「{name}」')
        self.gw_wait_refresh()
        return True

    def gw_post_text(self, name: str) -> bool:
        """逐字向客户端窗口投递 `WM_CHAR`，不抢焦点。

        桌面客户端接受纯 Unicode 字符（实测中文无需输入法即可上屏），
        因此不发按键码、不做输入法模拟。

        :return: 是否已投递；窗口句柄为空时返回 False
        """
        from win32api import PostMessage
        from win32con import WM_CHAR

        hwnd = self.device.root_handle_num
        if not hwnd:
            return False
        for char in name:
            PostMessage(hwnd, WM_CHAR, ord(char), 1)
            sleep(self.GW_TYPE_INTERVAL)
        return True

    def gw_input_ink(self) -> int:
        """搜索框 ROI 内的墨点数，用来判断框里有没有文字。

        空框实测 0、四个汉字约 238，所以不必为「文字是否上屏」新增素材。
        """
        self.screenshot()
        x, y, w, h = self.C_DT_GW_INPUT_SEARCH.roi_front
        box = self.device.image[y + 6:y + h - 4, x + 6:x + w - 6].astype(np.int32)
        return int((box.sum(axis=2) < self.GW_INK_LUMA_TOTAL).sum())

    def gw_wait_refresh(self) -> None:
        """点「搜索」后等列表刷新。

        只做定时轮询：出现「暂无祈愿消息」立即放行，否则等满 `GW_SEARCH_WAIT`。
        谁在这一行、赠与按钮在哪由 `process_donate` 判定，这里不抢它的活，
        也避免把慢刷新误判成搜索失败。
        """
        timer = Timer(self.GW_SEARCH_WAIT).start()
        while not timer.reached():
            sleep(0.2)
            self.screenshot()
            if self.appear(self.I_DT_GW_SEARCH_EMPTY):
                return
