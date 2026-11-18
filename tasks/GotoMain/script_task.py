# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import datetime
from time import sleep

from module.base.timer import Timer
from module.exception import (
    GameNotRunningError,
    GamePageUnknownError,
    GameStuckError,
    GameTooManyClickError,
    TaskEnd,
)
from module.logger import logger
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main
from tasks.Restart.server_update import (
    delay_pending_tasks_for_server_update,
    is_server_update_window,
)


class ScriptTask(GameUi):

    # 点击回庭院图标后等待庭院渲染完成的上限，单位秒。
    ICON_ARRIVE_TIMEOUT = 8.0

    def run(self) -> None:
        self.device.stuck_record_clear()
        try:
            # 快速通道放在 try 内：与 goto_page 共用同一套异常保护，
            # 避免截图/识别环节的异常绕过停服窗口的异常兑换直接冒到 Scheduler。
            if self._goto_main_by_icon():
                raise TaskEnd
            self.goto_page(page_main)
        except TaskEnd:
            raise
        except (GamePageUnknownError, GameStuckError, GameTooManyClickError, GameNotRunningError) as e:
            # 异常兑换：若处于停服窗口期，把"游戏页面异常 / 卡死 / 未运行"统一延后而不是抛回 Scheduler 触发 Restart
            if is_server_update_window() and self._delay_for_server_update(
                reason=f'{type(e).__name__} during goto_main inside server update window'
            ):
                raise TaskEnd('Goto main aborted due to server update window')
            raise e

        raise TaskEnd

    def _goto_main_by_icon(self) -> bool:
        """先试一次左上角「回庭院」图标，命中即直达庭院，省掉整条导航路径。

        该图标位于左上角返回箭头右侧，点下去会切回庭院场景。切场景耗时远长于
        普通弹窗关闭，因此这里单独轮询等待，不与 UnknownCloser 的 0.8~1.5 秒共用。

        Returns:
            是否已确认停在庭院主页；False 时调用方按原逻辑走 goto_page。
        """

        if not self.appear_then_click(self.I_UI_GOTO_MAIN, interval=0):
            return False
        timer = Timer(self.ICON_ARRIVE_TIMEOUT).start()
        while not timer.reached():
            self.screenshot()
            # 与 page_main 的识别条件保持一致：主界面标志命中、式神录标志未命中。
            if self.appear(self.I_CHECK_MAIN) and not self.appear(self.I_CHECK_MAIN_SET):
                logger.info('Goto main by icon: page_main arrived')
                return True
            sleep(0.5)
        logger.warning(
            f'回庭院图标已点击，但 {self.ICON_ARRIVE_TIMEOUT:.0f} 秒内未确认到达 page_main，交回 goto_page'
        )
        return False

    def _delay_for_server_update(self, reason: str) -> bool:
        delay_target = delay_pending_tasks_for_server_update(self.config, reason=reason)
        outcome = {
            'task': 'GotoMain',
            'status': 'server_update_delayed',
            'wait_until': delay_target,
        }
        self.config.task_runtime_outcome = outcome
        return True
