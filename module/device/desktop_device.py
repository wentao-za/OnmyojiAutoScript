# This Python file uses the following encoding: utf-8
"""桌面客户端（script.desktop.enable）在 Device 层的生命周期策略。

窗口/进程的实现细节在 module/device/method/desktop_impl.py 的 DesktopWindow，
这里只管「什么时候把客户端拉起来、什么时候算它在运行」。
"""
from module.device.method.desktop_impl import (
    desktop_pid_alive, desktop_window_titles, desktop_write_instance_state,
    find_desktop_window_by_pid)
from module.exception import GameInputStaleError
from module.logger import logger
import hashlib
import time


class DesktopDevice:
    # 输入失效效果面探针（坑位 46 方案 C）：同一控制名连续 N 次点击、跨越
    # N 个不同截图帧、画面零变化，即判定前 N-1 次点击零视觉响应。
    DESKTOP_PROBE_MIN_CLICKS = 3
    # 截图抽样步长：1280x720 每 8 像素取一点，签名输入约 43KB，md5 毫秒级
    DESKTOP_PROBE_SAMPLE_STRIDE = 8

    def _desktop_probe_signature(self):
        """当前截图的抽样签名，用于效果面比对（不依赖消息通道）。"""
        image = getattr(self, 'image', None)
        if image is None:
            return None
        stride = self.DESKTOP_PROBE_SAMPLE_STRIDE
        return hashlib.md5(image[::stride, ::stride].tobytes()).hexdigest()

    def _desktop_click_probe(self, control_name):
        """受控点击后的输入失效判定与分级恢复；仅桌面实例生效。

        判定只用截图比对，不向窗口发送任何探活消息——忙等态下消息可能被
        静默吞掉且不报错，发送面检测不可靠（坑位 46）。随机点击（SAFE_RANDOM_
        CLICK 等）设计上不要求画面变化、裸坐标点击（control_name='Click'）
        语义过宽，均不参与判定。配置缺 script.desktop 组（模板未升级）时
        is_desktop 会抛 AttributeError，视作非桌面、探针不启用。
        """
        try:
            enabled = self.is_desktop
        except AttributeError:
            enabled = False
        if not enabled:
            return
        if 'RANDOM' in control_name or control_name == 'Click':
            return
        sig = self._desktop_probe_signature()
        frame = getattr(self, 'image_frame_id', None)
        if sig is None or frame is None:
            return
        state = getattr(self, '_desktop_probe_state', None)
        if state is None:
            state = self._desktop_probe_state = {'name': None, 'frames': [], 'level': 0}
        if state['name'] != control_name or not state['frames']:
            state['name'] = control_name
            state['frames'] = [(frame, sig)]
            return
        first_sig = state['frames'][0][1]
        last_frame = state['frames'][-1][0]
        if sig != first_sig or frame == last_frame:
            # 画面发生过变化（点击有效）或尚未出现新截图帧（同帧重复点击），
            # 重新计数；画面变化同时把恢复阶梯清零
            if sig != first_sig:
                state['level'] = 0
            state['frames'] = [(frame, sig)]
            return
        state['frames'].append((frame, sig))
        if len(state['frames']) < self.DESKTOP_PROBE_MIN_CLICKS:
            return
        # 连续 3 次同名点击、跨 3 个不同截图帧、画面零变化：前两次点击均无效
        state['frames'] = [(frame, sig)]
        self._desktop_input_stale_recover(control_name)

    def _desktop_input_stale_recover(self, control_name):
        """输入失效三级恢复（坑位 46 方案 C）：

        ① 还原最小化窗口并按初始化同源逻辑重绑 PID/句柄（排除最小化与句柄
           过期两类可自愈因素）；
        ② 冷却 5s 后放行重试（吸收客户端短暂忙等）；
        ③ 仍无效则判定忙等态不可自愈，抛 GameInputStaleError 走 script.py 的
           GameStuckError 恢复路径（Restart，桌面实例为 force kill 客户端），
           不必等 device 层全局 10 次点击硬限被动触发。

        ①是尽力而为：重绑失败只告警不中断，后续两级仍会推进。任意一次点击
        产生画面变化即视为恢复成功，等级清零。
        """
        state = self._desktop_probe_state
        state['level'] += 1
        if state['level'] == 1:
            logger.warning(f'Desktop input possibly ineffective (control={control_name}, '
                           f'{self.DESKTOP_PROBE_MIN_CLICKS - 1} clicks no visual response), '
                           f'restore window and rebind')
            self.desktop_window_restore_if_minimized()
            try:
                pid, hwnd = self._desktop_locate_window()
                if pid and hwnd:
                    self.desktop_bind_pid(pid, hwnd, just_launched=False)
            except Exception as e:
                logger.warning(f'Desktop handle rebind failed (recovery ladder continues): {e}')
            return
        if state['level'] == 2:
            logger.warning(f'Desktop input still ineffective (control={control_name}), '
                           f'cooldown 5s then retry')
            time.sleep(5)
            return
        logger.error(f'Desktop window_message input stale (control={control_name}), '
                     f'recovery ladder exhausted, force restart required')
        state['level'] = 0
        raise GameInputStaleError(f'Desktop input stale: control={control_name}')

    # 登录态：OAS 自动启动的客户端停在 MPay 登录页时为 False，登录流程成功后
    # 置 True，运行期发现弹窗置回 False。默认 True 表示假设已在游戏中。
    _desktop_login_done = True

    def _desktop_ensure_launched(self) -> bool:
        """确保客户端可用：进程已死才拉起，进程活着只等窗口就绪。

        判定顺序是关键——先看进程再看窗口。把「窗口不在」当成「客户端没在运行」
        会拉起多余实例，从此再也绑不回原来那个。窗口被最小化不算没运行，
        截图方法会先还原再取帧。

        本会话没有存活 PID 时（刚启动、或上一个客户端已退出），先按「档案状态
        文件 → 唯一窗口枚举」认领已在运行的实例——按钮手动启动的实例就是这样
        被接上的；认领不到才拉起新的。
        """
        pid = self.desktop_pid()
        if pid is not None and desktop_pid_alive(pid):
            if self.desktop_window_exists():
                return True
            return self._desktop_wait_window_ready(pid)
        located_pid, hwnd = self._desktop_locate_window()
        if located_pid:
            if not hwnd and not self._desktop_wait_window_ready(located_pid):
                return False
            if not hwnd:
                hwnd = find_desktop_window_by_pid(
                    located_pid, desktop_window_titles(self.config))
            # 认领的是已存在的窗口，可能是用户手动开的且已登录，
            # 登录态由 find_desktop_login_popup 实测，不能假设未登录
            self.desktop_bind_pid(located_pid, hwnd, just_launched=False)
            return True
        return self.launch_desktop_client()

    def app_lost_login(self) -> bool:
        """客户端是否掉回了未登录态。

        MPay 登录窗存活时游戏画面上的一切页面标志都无效，发现即复位登录标记，
        使 app_is_running() 与任务前置检查能拦住后续流程。模拟器模式恒为 False。
        """
        if not self.is_desktop or not self.find_desktop_login_popup():
            return False
        logger.warning('Desktop MPay login popup present, client is not logged in')
        self.desktop_mark_logged_out()
        return True

    def desktop_mark_logged_in(self) -> None:
        """标记客户端已完成登录（登录流程成功后调用）。"""
        if getattr(self, '_desktop_login_done', True):
            return
        self._desktop_login_done = True
        self._desktop_sync_login_state(True)

    def desktop_mark_logged_out(self) -> None:
        """标记客户端处于未登录态。"""
        if getattr(self, '_desktop_login_done', True) is False:
            # 运行期每帧都会探测登录窗，状态没变就不重复落盘
            return
        self._desktop_login_done = False
        self._desktop_sync_login_state(False)

    def _desktop_sync_login_state(self, logged_in: bool) -> None:
        """把登录态写回档案状态文件，供新绑定的实例认领时判断。"""
        pid = self.desktop_pid()
        profile = self.desktop_profile_dir()
        if pid is None or profile is None:
            return
        desktop_write_instance_state(profile, pid, logged_in=logged_in)

    def _init_desktop(self) -> None:
        """桌面客户端模式初始化：跳过模拟器健康检查/full_recovery。"""
        logger.info('Desktop client mode: skip emulator health check and full recovery')
        if self.config.script.device.screenshot_method == 'auto':
            # auto 是「自动选择」的语义，桌面模式自动落到后台截图，无需告警
            self.config.script.device.screenshot_method = 'window_background'
        elif self.config.script.device.screenshot_method != 'window_background':
            logger.warning(
                f'Desktop mode requires screenshot_method=window_background, '
                f'current={self.config.script.device.screenshot_method}, overriding'
            )
            self.config.script.device.screenshot_method = 'window_background'
        if self.config.script.device.control_method != 'window_message':
            logger.warning(
                f'Desktop mode requires control_method=window_message, '
                f'current={self.config.script.device.control_method}, overriding'
            )
            self.config.script.device.control_method = 'window_message'
        self.config.save()
        self.screenshot_interval_set()
        # 桌面客户端的渲染经过 DPI 缩放重采样，相对模板截图环境（模拟器原生
        # 1280x720）存在约 1px 的相位差；仓库里约 1/3 的模板 roi 与模板等宽，
        # matchTemplate 结果只剩 1 列、横向无法平移补偿，这点相位差就足以把
        # 「元素存在」判成「不存在」。这里统一外扩 2px 吸收之，模拟器模式保持 0。
        from module.atom.image import DESKTOP_ROI_MATCH_PAD
        self.image_roi_padding = DESKTOP_ROI_MATCH_PAD
        # 客户区校准到 1280x720 保证识别 1:1。窗口存在性已由 __init__ 的
        # _desktop_ensure_launched 保证，运行期掉了由 Restart 负责重拉
        self.desktop_window_set_size()
