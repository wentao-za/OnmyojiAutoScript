# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
"""超鬼王任务分派层。

只做三件事，业务一律下沉到子模块：

1. 读运行模式列表（`parse_modes` 已保证门票刷取在前）并逐个分派；
2. 为每个分支重置 `start_time`，让各自的限时从自己开始算
   （门票刷满不该吃掉战斗那 45 分钟）；
3. 统一收尾：`set_next_run` + `finish_task`。

子模块职责：
- `ticket_farming`：前置刷票、反应式补票、召唤队列开窗；
- `boss_battle`：合成、召唤、换魂、强力、战斗、好友副本；
- `assistance`：六星上供与分享；
- `md_ui`：界面探针与弹窗原语（纯叶子）。
"""
from datetime import datetime

from module.logger import logger

from tasks.MetaDemon.assistance import run_assistance
from tasks.MetaDemon.boss_battle import BossBattle
from tasks.MetaDemon.config import MetaDemonMode
from tasks.MetaDemon.ticket_farming import TICKET_MODES, run_ticket_farming


class ScriptTask(BossBattle):
    """超鬼王"""

    def run(self):
        # 调度基准：整个任务的开跑时刻。`set_next_run` 用它推算下次运行时间，
        # 中间会被各分支覆盖，收尾前必须还原（见函数末尾）。
        started_at = self.start_time
        self.conf = self.config.meta_demon
        modes = self.conf.meta_demon_config.mode
        logger.hr('超鬼王', 0)
        logger.info(f'运行模式：{" → ".join(mode.value for mode in modes)}')

        success = True
        for mode in modes:
            self.start_time = datetime.now()
            if mode in TICKET_MODES:
                # 前置优先：先刷满票，再进战斗（D5）
                run_ticket_farming(self, mode)
            elif mode == MetaDemonMode.BOSS_BATTLE:
                success = self.run_battle_loop()
            elif mode == MetaDemonMode.BOSS_ASSIST:
                success = run_assistance(self)
            else:
                logger.warning(f'未知运行模式 {mode}，跳过')

        self.start_time = started_at
        self.set_next_run(task='MetaDemon', success=success)
        # finish_task 内部 raise TaskEnd 收束整条调用栈，故此处不再 return
        self.finish_task()


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()
