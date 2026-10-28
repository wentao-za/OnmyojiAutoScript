from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.default_pages import page_collection, page_guild, page_main
from tasks.GameUi.page_definition import Page
from tasks.GlobalGame.assets import GlobalGameAssets
from tasks.WeeklyTrifles.assets import WeeklyTriflesAssets
from module.logger import logger

import time

page_shikigami_collection = Page(WeeklyTriflesAssets.I_WT_SHARE)
page_shikigami_collection.add_enter_failure_hooks(GlobalGameAssets.I_UI_CANCEL_SAMLL)
page_collection.connect(
    page_shikigami_collection,
    GameUiAssets.I_CHECK_COLLECTION,
    key="page_collection->page_shikigami_collection",
)
page_shikigami_collection.connect(
    page_collection,
    GlobalGameAssets.I_UI_BACK_YELLOW,
    key="page_shikigami_collection->page_collection",
)

page_shikigami_share = Page(WeeklyTriflesAssets.I_WT_COLLECT_WECHAT)
page_shikigami_collection.connect(
    page_shikigami_share,
    WeeklyTriflesAssets.I_WT_SHARE,
    key="page_shikigami_collection->page_shikigami_share",
)
page_shikigami_share.connect(
    page_shikigami_collection,
    GlobalGameAssets.I_UI_BACK_RED,
    key="page_shikigami_share->page_shikigami_collection",
)

def _goto_tf_action(self):
    """边 action：在阴阳寮页找到 I_GUILD_GOTO_TF 并点击；找不到就滑动再试。"""
    goto_tf = WeeklyTriflesAssets.I_GUILD_GOTO_TF
    # 1) 第一次直接找 + 点
    if self.appear_then_click(goto_tf):
        return True

    # 2) 确认还在阴阳寮页，才允许滑动（否则直接放弃）
    if not self.match_page_once(page_guild):
        return False

    # 3) 滑动重试，每次滑完都复核是否还在阴阳寮页
    for _ in range(3):
        self.swipe(WeeklyTriflesAssets.S_WT_TF_SWIPE1)
        time.sleep(1)

        if self.appear_then_click(goto_tf):
            return True

    return False  # 滑到底还是没有，交给 on_enter_failure

def _goto_tf_failed(transition):
    """边级失败 hook：记录/兜底。"""
    logger.warning(f"[WARN] 未能在阴阳寮页找到摸鱼入口: {transition.key}")
    # 比如：重进 guild、切 tab、抛异常等

# 摸鱼行动页面
page_touch_fish = Page(WeeklyTriflesAssets.I_CHECK_TOUCH_FISH)
page_guild.connect(
    page_touch_fish,
    action=_goto_tf_action,
    # WeeklyTriflesAssets.I_GUILD_GOTO_TF,
    key="page_guild->page_touch_fish",
    on_enter_failure=[_goto_tf_failed],
)
page_touch_fish.connect(
    page_main,
    WeeklyTriflesAssets.I_WT_TF_GOTO_MAIN,
    key="page_touch_fish->page_main",
)
