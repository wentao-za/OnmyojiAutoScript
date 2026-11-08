import timeit
import numpy as np
from datetime import datetime

from module.base.timer import Timer
from module.config.utils import desktop_mode_enabled
from module.device.handle import Handle, WindowNode, handle_num2title, handle_title2num, is_handle_valid
from module.logger import logger
from module.base.utils import point2str
from module.exception import RequestHumanTakeover, GameStuckError
from tasks.base_task import BaseTask

from tasks.Hyakkiyakou.config import ScreenshotMethod, ControlMethod

def image_black(img) -> bool:
    for y, x in [(0, 0), (719, 1279), (719, 0), (0, 1279)]:
        if np.all(img[y, x] != 0):
            return False
    return True


class HyaDevice(BaseTask):
    """
    这个类主要是是优化截屏点击速度
    1. 使用特别的method
    2. 扔掉中间的冗余校验
    3. 考虑JIT加速
    我宣布世界上最好的 Linux 系统是 Windows
    """
    hya_screenshot_interval = Timer(0.2)  # 300ms
    hya_fs_check_timer = Timer(3 * 60)  # 五分钟跑不完就应该是出问题了
    _fallback_warned = False  # 降级通道告警只报一次，防止每帧刷屏

    def _warn_fallback_once(self, message: str) -> None:
        """降级运行告警只打一次。

        桌面模式或 handle 缺失时不存在 root_node，fast_screenshot / fast_click
        每帧都会回落到标准通道，逐帧告警在单局内可达数十条，会把真正的
        ERROR 淹掉。降级本身是预期行为，首帧报一次即可。
        """
        if self._fallback_warned:
            return
        self._fallback_warned = True
        logger.warning(message)

    def _ensure_root_node(self) -> None:
        """Ensure root_node is initialized on the Device.

        Handle.__init__ returns early without creating root_node in two cases:
        1. config.script.device.handle is empty, this rebuilds it on demand.
        2. desktop client mode, Handle binds PID/hwnd instead of a handle tree,
           root_node is absent by design, so there is nothing to rebuild.
        """
        if hasattr(self.device, 'root_node'):
            return
        # 桌面客户端模式（script.desktop.enable=True）由 Handle 绑定 PID/hwnd，
        # 全程不构建 root_node，这是设计如此、不是 handle 配置缺失，
        # 不应每帧打告警把真正的 ERROR 淹没
        if desktop_mode_enabled(self.device.config):
            return
        from module.config.config import Config
        root_handle = self.device.config.script.device.handle
        if not root_handle:
            logger.warning('Device handle config is empty, cannot build root_node')
            return
        root_handle_title = ''
        root_handle_num = 0
        if root_handle == 'auto':
            logger.info('Handle is auto, searching for emulator window')
            window_list = Handle.all_windows()
            root_handle_title = Handle.auto_handle_title(window_list)
            root_handle_num = handle_title2num(root_handle_title)
        else:
            try:
                root_handle_num = int(root_handle)
                if is_handle_valid(root_handle_num):
                    root_handle_title = handle_num2title(root_handle_num)
            except ValueError:
                if handle_title2num(root_handle) != 0:
                    root_handle_num = handle_title2num(root_handle)
                    root_handle_title = root_handle
        self.device.root_handle_title = root_handle_title
        self.device.root_handle_num = root_handle_num
        self.device.root_node = WindowNode(name=root_handle_title, num=root_handle_num)
        Handle.handle_tree(root_handle_num, self.device.root_node)
        logger.info(f'root_node initialized: title={root_handle_title}, num={root_handle_num}')

    def fast_screenshot(self, screenshot: ScreenshotMethod):
        self._ensure_root_node()
        self.hya_screenshot_interval.wait()
        self.hya_screenshot_interval.reset()
        if hasattr(self.device, 'root_node'):
            self.device.image = self.device.screenshot_window_background() if screenshot == ScreenshotMethod.WINDOW_BACKGROUND else self.device.screenshot_nemu_ipc()
        else:
            self._warn_fallback_once('root_node unavailable, falling back to standard screenshot')
            self.device.screenshot()
        self.device.image_frame_id = None
        if image_black(self.device.image):
            logger.error('Screenshot image is black, try again')
            raise RequestHumanTakeover('Screenshot image is black, try again')
        if self.hya_fs_check_timer.reached():
            logger.error('Fast screenshot check timer reached')
            logger.error('Five minutes have not ended, the game is probably stuck, please check the game')
            raise GameStuckError
        if self.config.script.error.save_error:
            self.device.screenshot_deque.append({'time': datetime.now(), 'image': self.device.image})
        return self.device.image

    def fast_click(self, x: int, y: int, control_method: ControlMethod = ControlMethod.WINDOW_MESSAGE) -> None:
        self._ensure_root_node()
        logger.info(
            'Click %s @ %s' % (point2str(x, y), 'Click')
        )
        if not hasattr(self.device, 'root_node'):
            self._warn_fallback_once('root_node unavailable, falling back to standard click')
            # 一局投豆 30+ 次全走这条降级路径，若用默认 control_name='Click' 会写进
            # click_record（deque(maxlen=15)），第 10 次必触 GameTooManyClickError。
            # 有 root_node 的快通道 click_window_message 本来就不计数，此处对齐之。
            # control_check=False 顺带跳过 handle_control_check 里的 stuck_record_clear()，
            # 这正好保住 detect_record 中的 BATTLE_STATUS_S 白名单（device.py:35），勿补回。
            self.device.click(x, y, control_check=False)
            return
        if control_method == ControlMethod.MINITOUCH:
            try:
                self.device.click_minitouch(x=x, y=y)
            except AttributeError:
                logger.warning('click_minitouch failed, falling back to standard click')
                self.device.click(x, y)
        else:
            try:
                self.device.click_window_message(x=x, y=y, fast=True)
            except AttributeError:
                logger.warning('click_window_message failed, falling back to standard click')
                self.device.click(x, y)

    def set_fast_screenshot_interval(self, interval: float):
        """

        @param interval: ms
        @return:
        """
        self.hya_screenshot_interval = Timer(interval / 1000.)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    hd = HyaDevice(c, d)

    # def screenshot():
    #     global hd
    #     # hd.fast_screenshot()
    #     hd.fast_click(420, 370)
    #     hd.fast_click(750, 400)
    # execution_time = timeit.timeit(screenshot, number=50)
    # print(f"执行总的时间: {execution_time * 1000} ms")

    from tasks.Hyakkiyakou.config import ScreenshotMethod
    hd.fast_screenshot(screenshot=ScreenshotMethod.WINDOW_BACKGROUND)
