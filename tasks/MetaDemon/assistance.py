# This Python file uses the following encoding: utf-8
"""超鬼王协助域：上供（只召唤六星、按名字分享给指定好友、等好友击杀）。

职责边界
--------
- 只做「把六星鬼王交出去」这件事：召唤六星 → 勾选指定好友 → 提交分享 → 轮询等待被击杀。
- **不挑战**任何鬼王（`tick` 永不进入战斗），**不切御魂**，**不处理补票**以外的门票逻辑
  —— 门票开窗与补票复用 `ticket_farming.ensure_ticket` / `exhaust_ticket`。

两条硬约束（来自上游 self 版，实测有效，保留）
------------------------------------------
1. 星级未知的鬼王一律不分享：好友鬼王与自家鬼王在界面上都显示「挑战」，
   星级无法从界面反查，只有刚由本进程召唤的那一只才知道是六星。
2. 分享提交前必须重新截图并确认勾选状态，「选择 → 截图 → 提交」不能跨帧复用，
   否则会把未完成的勾选当成已完成，重复分享同一只鬼王。
"""
import re
import time
from datetime import datetime

from module.atom.click import RuleClick
from module.atom.swipe import RuleSwipe
from module.exception import RequestHumanTakeover, TaskEnd
from module.logger import logger

from tasks.MetaDemon.config import AssistanceRole, BossType
from tasks.MetaDemon.ticket_farming import TicketStatus
import tasks.GameUi.task_pages as pages


class GhostOffering:
    """上供流程的状态机。所有动作都通过 `self.task`（BossBattle/ScriptTask）完成。"""

    def __init__(self, task):
        self.task = task
        self.settings = task.conf.assistance_config
        # 已经分享出去的那一代鬼王；与 task._boss_generation 比对，避免重复分享
        self.shared_generation = None
        self.waiting = False
        self.empty_frames = 0
        self.unknown_reported = False

    def within_limit(self):
        t = self.task
        return (datetime.now() - t.start_time < t.limit_time and
                t.total_count < t.limit_count and not t.State.done)

    # -------------------------------------------------------------- 好友列表

    @staticmethod
    def normalize_name(name):
        return re.sub(r'\s+', '', str(name))

    def friend_rows(self):
        """OCR 好友列表，返回 {规范化名字: [(left, top, width, height), ...]}。"""
        t = self.task
        x, y, _, _ = t.O_MD_FRIENDS.roi
        rows = {}
        for result in t.O_MD_FRIENDS.detect_and_ocr(t.device.image):
            name = self.normalize_name(result.ocr_text)
            box = result.box
            left, top = int(min(box[:, 0])) + x, int(min(box[:, 1])) + y
            width = max(1, int(max(box[:, 0]) - min(box[:, 0])))
            height = max(1, int(max(box[:, 1]) - min(box[:, 1])))
            rows.setdefault(name, []).append((left, top, width, height))
        return rows

    def selected_count(self):
        t = self.task
        return len(t.I_MD_FRIEND_SELECTED.match_all(t.device.image, frame_id=t.device.image_frame_id))

    def list_to_top(self):
        """反向滑动直到列表稳定在顶部，避免「只扫到半屏」漏掉好友。"""
        t = self.task
        swipe = t.S_MD_SWIPE_FRIEND_UP
        reverse = RuleSwipe(roi_front=swipe.roi_back, roi_back=swipe.roi_front,
                            mode='default', name='metademon_friend_list_to_top')
        previous = None
        stable = 0
        for _ in range(20):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            signature = tuple(self.friend_rows())
            stable = stable + 1 if signature == previous else 0
            if stable >= 2:
                return True
            previous = signature
            t.swipe(reverse, interval=1.2)
        return False

    def open_share_panel(self):
        t = self.task
        deadline = time.monotonic() + 12
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if t.appear(t.I_MD_INVITE_FRIENDS):
                return True
            t.appear_then_click(t.I_MD_INVITE, interval=1.5)
        return False

    def select_friend(self, name, area):
        """点一行好友，并确认勾选数 +1；未确认就整体失败。"""
        t = self.task
        before = self.selected_count()
        t.click(RuleClick(roi_front=area, roi_back=area, name='metademon_offering_friend'))
        deadline = time.monotonic() + 4
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            if self.selected_count() == before + 1:
                logger.info(f'鬼王上供已选好友: {name}')
                return True
        logger.warning(f'好友 {name} 点击后未确认选中，停止本次分享')
        return False

    def select_friends(self):
        t = self.task
        wanted = {self.normalize_name(name): name for name in self.settings.friends}
        if not wanted or not self.list_to_top():
            return False
        # 先把整份列表扫完再选：否则屏幕外的残留勾选会带进未配置的收件人
        previous = None
        stable = 0
        available = set()
        for _ in range(30):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS) or self.selected_count():
                logger.warning('分享列表存在已有选择或已关闭，请清空好友选择后重试')
                return False
            rows = self.friend_rows()
            if any(len(rows.get(name, [])) > 1 for name in wanted):
                logger.warning('好友名字有多个相同识别结果，无法唯一选择')
                return False
            available.update(rows)
            signature = tuple(rows)
            stable = stable + 1 if signature == previous else 0
            if stable >= 2:
                break
            previous = signature
            t.swipe(t.S_MD_SWIPE_FRIEND_UP, interval=1.2)
        else:
            logger.warning('好友列表扫描超过限制，停止分享')
            return False
        missing = wanted.keys() - available
        if missing:
            logger.warning('未找到指定好友: ' + '; '.join(wanted[name] for name in missing))
            return False
        if not self.list_to_top():
            return False
        remaining = dict(wanted)
        for _ in range(30):
            if not self.within_limit():
                return False
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS):
                return False
            for name in tuple(remaining):
                # 勾选会改变行的顺序，每个名字都要在最新截图上重新定位
                rows = self.friend_rows()
                if name not in rows:
                    continue
                if len(rows[name]) != 1 or not self.select_friend(remaining[name], rows[name][0]):
                    return False
                del remaining[name]
            if not remaining:
                return True
            t.swipe(t.S_MD_SWIPE_FRIEND_UP, interval=1.2)
        return False

    # ---------------------------------------------------------------- 分享

    def share(self):
        t = self.task
        if t.cur_boss_type != BossType.SIX_STARS:
            logger.warning('仅允许分享本次已确认召唤的六星鬼王')
            return False
        if not self.open_share_panel() or not self.select_friends():
            return False
        # 选择 → 重新截图 → 提交，绝不提交不完整的勾选
        t.screenshot()
        if not t.appear_then_click(t.I_MD_INVITE_FRIENDS, interval=1.5):
            return False
        deadline = time.monotonic() + 10
        while self.within_limit() and time.monotonic() < deadline:
            t.screenshot()
            if not t.appear(t.I_MD_INVITE_FRIENDS) and (
                    t.appear(t.I_MD_FIRE) or t.appear(t.I_MD_SUMMON_BOSS) or t.appear(t.I_MD_SETTLING)):
                logger.info('六星鬼王分享流程完成，等待好友击杀')
                return True
        logger.warning('未确认分享面板关闭，停止本次上供，避免重复分享')
        return False

    # ---------------------------------------------------------------- 主循环

    def tick(self):
        """推进一拍。返回 False 表示本次运行应当停止；永不进入战斗。"""
        t = self.task
        if t.appear(t.I_UI_REWARD):
            t.click(t.C_UI_REWARD, interval=0.8)
            return True
        if t.appear(t.I_MD_SETTLING):
            self.empty_frames = 0
            return True
        if t.appear(t.I_MD_FIRE):
            self.empty_frames = 0
            if t.cur_boss_type != BossType.SIX_STARS:
                if not self.unknown_reported:
                    logger.warning('已有鬼王星级未确认，不分享、不挑战；等待其消失后仅召唤六星')
                    self.unknown_reported = True
            elif self.shared_generation != t._boss_generation:
                if not self.share():
                    return False
                self.shared_generation = t._boss_generation
            self.waiting = True
            return True
        if not t.appear(t.I_MD_SUMMON_BOSS):
            self.empty_frames = 0
            return True
        # 「召唤鬼王」按钮消失要连续两帧才算数：单帧多半是转场残影
        self.empty_frames += 1
        if self.empty_frames < 2:
            return True
        if self.waiting:
            if self.shared_generation is not None:
                t.total_count += 1
                logger.info(f'上供鬼王已结束: {t.total_count}/{t.limit_count}')
            self.waiting = False
            self.shared_generation = None
            t.cur_boss_type = None
            self.unknown_reported = False
            if not self.within_limit():
                return True
        self.empty_frames = 0
        # 只召唤六星：序列里只放 SIX_STARS，其余星级不碰
        status = t.ensure_ticket()
        if status is TicketStatus.BLOCKED:
            # 已结束（无票/已委派补票）时返回 True 让循环自然退出；
            # 弹窗 15 秒开不出来属于环境异常，按失败停止
            return t.State.done
        if status is TicketStatus.SUMMON:
            if not t.summon_and_fire(sequence=[BossType.SIX_STARS]):
                t.exhaust_ticket()
                return t.State.done
        return True


def run_assistance(task) -> bool:
    """鬼王上供主流程。

    :return: 本次运行是否正常完成；由分派层（ScriptTask.run）据此收尾。
    """
    settings = task.config.meta_demon.assistance_config
    if settings.role == AssistanceRole.ASSIST:
        logger.warning('鬼王协助身份尚未实现，本次不执行')
        return False
    if not settings.friends:
        raise RequestHumanTakeover('鬼王上供需要填写分享好友名字')
    task.before_run()
    task.goto_page(pages.page_meta_demon_boss)
    offering = GhostOffering(task)
    while offering.within_limit():
        # 等别人来杀是刻意的静止，不能算卡死
        task.device.stuck_record_clear()
        task.screenshot()
        if not offering.tick():
            logger.warning('鬼王上供流程未完成，结束本次运行')
            return False
        remaining = max(0, (task.limit_time - (datetime.now() - task.start_time)).total_seconds())
        time.sleep(min(settings.poll_interval, remaining))
    return True
