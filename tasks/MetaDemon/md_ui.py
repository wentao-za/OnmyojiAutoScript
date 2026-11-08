# This Python file uses the following encoding: utf-8
"""超鬼王任务的界面探针与弹窗处置原语。

**本模块是纯叶子**：只依赖框架（`GameUiAssets` / `ipages` / `Duration`），
不 import 任何业务模块（boss_battle / ticket_farming / assistance / synthesis），
否则会形成 `task_pages -> MetaDemon.assets` 之外的又一条环。

职责边界：只描述「界面现在长什么样、点哪里能把它恢复」，**不含业务决策**。
"15 秒内开不出就跳过本轮" 这类策略留在调用方（ticket_farming），
所以这里只有原语，没有状态机循环。
"""
import time

from module.logger import logger
from module.ocr.sub_ocr import Duration
from tasks.GameUi.assets import GameUiAssets
import tasks.GameUi.task_pages as ipages


class MdUiMixin:
    """界面探针 + 弹窗原语。由 ScriptTask 混入，所有素材经 `self.I_MD_*` 取得。"""

    # 昨日狩猎奖励面板状态
    _reward_claim_ts: float = 0.0
    _reward_fail_until: float = 0.0
    _reward_blank_cnt: int = 0
    # 意外弹窗连点限流
    _popup_close_cnt: int = 0
    _popup_close_last: float = 0.0
    # 驻留期残余弹窗自愈的独立限流（不与上面的导航兜底互相消耗额度）
    _idle_popup_cnt: int = 0
    _idle_popup_window: float = 0.0

    def reset_popup_state(self) -> None:
        """每轮任务开始时清空弹窗相关的跨帧状态。"""
        self._reward_claim_ts = 0.0
        self._reward_fail_until = 0.0
        self._reward_blank_cnt = 0
        self._popup_close_cnt = 0
        self._popup_close_last = 0.0
        self._idle_popup_cnt = 0
        self._idle_popup_window = 0.0

    # ------------------------------------------------------------------ 探针

    def meta_page_visible(self) -> bool:
        """超鬼王主界面 / 鬼王界面是否已恢复（弹窗是否已彻底关掉）。"""
        return (
            self.appear(GameUiAssets.I_PAGE_METADEMON_MD_CHECK_MAIN_PAGE, interval=0.3)
            or self.appear(GameUiAssets.I_PAGE_METADEMON_CHECK_BOSS_PAGE, interval=0.3)
        )

    def is_meta_demon_boss_page(self) -> bool:
        """鬼王页判据：召唤鬼王 / 切换门票 / 挑战按钮任一出现。"""
        return (self.appear(self.I_MD_SUMMON_BOSS)
                or self.appear(self.I_MD_SWITCH_TICKET)
                or self.appear(self.I_MD_FIRE))

    @staticmethod
    def ocr_seconds(text) -> int | None:
        """把 OCR 出来的「01:28:00」「01:28:00后逃跑」解析成秒数，失败返回 None。

        `mode="Full"` 的 OCR 空读返回结构化的 `(0, 0, 0, 0)`，直接丢给
        `Duration.parse_time` 会打一条 `Invalid duration` warning（2026-11-06
        驻留期每 8 秒刷一条）。这里先按结构判空：全零就等价于没读到，直接 None，
        不进入解析器。原有 `seconds > 0` 的兜底保留。
        """
        if not text:
            return None
        if isinstance(text, (tuple, list)) and all(str(i).strip() in ('', '0') for i in text):
            return None
        seconds = int(Duration.parse_time(str(text)).total_seconds())
        return seconds if seconds > 0 else None

    # -------------------------------------------------------- 召唤队列弹窗原语

    def summon_panel_opened(self) -> bool:
        """「裂变之眼召唤队列」弹窗是否已稳定打开。

        有票时弹窗底部是「立即召唤」，无票时是「暂无裂变之眼」+「前往获取」，
        两者任一命中都说明弹窗已经开出来了。**单判据会把"无票但面板已开"
        误判成"面板没开"，进而重复点入口把面板点关。**
        """
        return (self.appear(self.I_MD_GET_BOSS, interval=0.6)
                or self.appear(self.I_MD_TICKET_EMPTY, interval=0.6))

    def press_summon_entry(self) -> bool:
        """点一次「切换门票」入口。点成功返回 True，由调用方控制补点节奏。"""
        return self.appear_then_click(self.I_MD_SWITCH_TICKET, interval=0.6)

    def close_summon_panel(self) -> None:
        """关闭召唤队列弹窗，失败不阻塞后续流程。"""
        if self.summon_panel_opened():
            self.ui_click_until_disappear(self.I_MD_CLOSE_POPUP, interval=0.6, timeout=8)

    # ------------------------------------------------------------ 弹窗处置

    def claim_yesterday_reward(self) -> bool:
        """处理进入超鬼王时弹出的「昨日狩猎奖励」面板。

        实测（2026-11-05 10:45 / 10:48 三次同帧崩溃）：
        - 面板盖住右上检查位，page_meta_demon 只有 0.254（阈值 0.8），页识别必然失败；
        - 面板没有红 X（I_MD_CLOSE_POPUP 只有 0.430），唯一出口是下方「领取」；
        - 旧素材命中率只有 0.342（旧版按钮），所以「点返回键空转到
          GameTooManyClickError」是必然结局。重裁后 226x55 素材实测 1.000，
          178 张快照里只有这 3 帧命中、其余最高 0.437。
        - 「领取」按钮只在面板态出现，消失即代表面板已关（坑 37：判据不能选两态都出现的素材）。

        点击「领取」后由标准领奖函数 `ui_get_reward()`（base_task.py）扛完整段结算：
        点「领取」→ 等「获得奖励」横幅出现 → 点到横幅消失才返回（实测名次
        结算页 page_chess_result 上 I_UI_REWARD 0.9853，横幅就是出口）。
        旧实现点到「领取」消失就撒手，而结算页判据里也含 I_UI_REWARD，页识别
        落到 page_chess_result 后主循环无人接管、阶段二逃生又被 meta_page_visible()
        误判（CHECK_BOSS_PAGE 在结算页 0.9974，活动页头共用元素）关闭，
        60s 零点击 → GameStuckError（2026-11-07 oas4）。
        阶段二保留为兜底：仅当 ui_get_reward 异常返回、页面仍未恢复时补点空白。
        """
        now = time.time()
        if now < self._reward_fail_until:
            return False

        # 阶段一：面板在 → 点「领取」并收掉后续「获得奖励」结算页
        if self.appear(self.I_MD_GET_YESTERDAY_REWARD, interval=0.4):
            logger.info('识别到「昨日狩猎奖励」面板，点击领取')
            # 标准领奖流程：ui_get_reward 内部点 click_image → 等 I_UI_REWARD
            # 横幅（10s 未出现则告警退出，不抛错）→ 点到横幅消失。
            # click_interval=1.2 与旧版一致，避免撞 device 同按钮连击保护。
            self.ui_get_reward(self.I_MD_GET_YESTERDAY_REWARD, click_interval=1.2)
            self._reward_claim_ts = time.time()
            self._reward_blank_cnt = 0
            return True
        # 阶段二：领取已点过、面板消失，但页面还没恢复——奖励页点空白收尾
        if self._reward_claim_ts and now - self._reward_claim_ts < 10:
            if self.meta_page_visible():
                self._reward_claim_ts = 0.0
                return False
            if self._reward_blank_cnt < 2:
                self._reward_blank_cnt += 1
                logger.info(f'奖励页点空白关闭（第 {self._reward_blank_cnt} 次）')
                self.click(ipages.random_click(), interval=0.8)
                time.sleep(1.2)
                return True
        elif self._reward_claim_ts:
            # 10 秒窗口过期仍未确认：清零，等下一次真正弹面板时重新计
            self._reward_claim_ts = 0.0
        return False

    def close_unexpected_popup(self) -> bool:
        """关闭遮挡界面的意外弹窗（礼包、公告、商城推送等），点右上角红 X。

        素材复用弹窗通用关闭按钮 I_MD_CLOSE_POPUP。实测：
        - 「宠梦礼包」全屏弹窗 0.925（命中点 1201,116，就在 X 上）
        - 庭院 / 探索 / 鬼王主界面 / 活动页 全部 ≤ 0.505，不会误点
        只在确实匹配到 X 时才动作（阈值 0.8），并等一小段淡出时间。
        同一时间窗内最多连点 5 次：再点下去会触发 device 层同位置 10 次的
        GameTooManyClickError 重启，那是更差的结果。
        """
        if not self.appear(self.I_MD_CLOSE_POPUP, interval=0.4):
            self._popup_close_cnt = 0
            return False
        now = time.time()
        if now - self._popup_close_last > 60:
            self._popup_close_cnt = 0
        if self._popup_close_cnt >= 5:
            return False
        self._popup_close_cnt += 1
        self._popup_close_last = now
        logger.warning('识别到遮挡界面的意外弹窗，点击右上角红 X 关闭')
        self.appear_then_click(self.I_MD_CLOSE_POPUP, interval=0.6)
        time.sleep(1.2)  # 等弹窗淡出，避免下一帧判到半透明残影
        return True

    def close_residual_popup(self) -> bool:
        """驻留期残余弹窗自愈：命中有界重试关闭，返回 True 表示本拍处理过弹窗。

        与 `close_unexpected_popup()` 的分工：
        - 那个服务「判页失败的未知页」兜底，单拍只点一下、宁可放弃也不连点；
        - 这个服务驻留态（可达 limit_time 的长周期）。召唤队列弹窗若漏关会一直压着
          好友分享素材的 ROI 右缘（约 18px），整段驻留都在漏判好友副本，
          所以允许一次调用内重试，把「首击被淡入动画吞掉」的情形捞回来。

        限流上限按 device 层反推：`click_record` 是 `deque(maxlen=15)` 的滑动窗口，
        同一按钮出现 ≥10 次就抛 `GameTooManyClickError`（`module/device/device.py`）。
        这里 60 秒窗口内最多 3 次调用、每次最多 2 击 = 6 次，留 4 次余量给
        `start_battle()` 里喝茶失败路径点同一个红 X。到顶后返回 False 静默降级：
        界面继续被压着，但绝不会因为连点把进程重启掉。

        窗口锚点用**窗口起点**而非最后一次点击时刻：若每次命中都刷新锚点，窗口永不滚动，
        3 次用完后整段驻留就再也不关弹窗了（「点掉一个又来一个」的场景会被误伤）。
        60 秒后重置计数，可以重新自愈。

        判据直接用 `I_MD_CLOSE_POPUP` 本身，**不经过 `summon_panel_opened()`**：
        `appear` 的计时器 key 是素材名，那个探针判的 `I_MD_TICKET_EMPTY` 可能刚被
        `judge_drop_progress()` 命中并重置，再判会稳定返回 False —— 2026-11-06
        召唤队列弹窗漏关正是这个原因（`appear` 的 interval 冷却，见 tasks/base_task.py）。
        """
        if not self.appear(self.I_MD_CLOSE_POPUP, interval=0.4):
            self._idle_popup_cnt = 0
            return False
        now = time.time()
        if now - self._idle_popup_window >= 60:
            self._idle_popup_cnt = 0
            self._idle_popup_window = now
        if self._idle_popup_cnt >= 3:
            return False
        self._idle_popup_cnt += 1
        logger.warning(f'驻留期识别到残余弹窗，有界重试关闭（本分钟第 {self._idle_popup_cnt}/3 次）')
        closed = self.ui_click_until_disappear(
            self.I_MD_CLOSE_POPUP,
            interval=0.6,
            timeout=6,
            max_clicks=2,
            confirm_frames=2,
        )
        time.sleep(1.0)  # 等淡出，避免下一拍判到半透明残影又白点一次
        if not closed:
            logger.warning('残余弹窗 6 秒内未关闭，本拍放弃（已点 2 次，不再连点）')
        return True
