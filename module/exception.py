class CampaignEnd(Exception):
    pass


class MapDetectionError(Exception):
    pass


class MapWalkError(Exception):
    pass


class MapEnemyMoved(Exception):
    pass


class CampaignNameError(Exception):
    pass


class ScriptError(Exception):
    # This is likely to be a mistake of developers, but sometimes a random issue
    pass


class ScriptEnd(Exception):
    pass


class GameStuckError(Exception):
    pass


class GameInputStaleError(GameStuckError):
    """window_message 输入通道失效（坑位 46）：桌面客户端忙等态下窗口消息被
    静默丢弃、截图正常，点击无任何视觉响应且恢复阶梯用尽。继承 GameStuckError
    以复用 script.py 既有 Restart 恢复路径（桌面实例为 force kill 客户端）。"""


class GameBugError(Exception):
    # An error has occurred in Azur Lane game client. Alas is unable to handle.
    # A restart should fix it.
    pass


class GameTooManyClickError(Exception):
    pass


class EmulatorNotRunningError(Exception):
    pass


class GameNotRunningError(Exception):
    pass


class GamePageUnknownError(Exception):
    pass


class RequestHumanTakeover(Exception):
    # Request human takeover
    # Alas is unable to handle such error, probably because of wrong settings.
    pass

class TaskEnd(Exception):
    pass


class NetworkRecoveryRequired(Exception):
    """Unwind task state before network recovery."""

    def __init__(self, reconnect=False):
        self.reconnect = reconnect
        super().__init__("Network reconnect required" if reconnect else "Network restart required")
