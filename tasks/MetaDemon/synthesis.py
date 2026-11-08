# This Python file uses the following encoding: utf-8
"""超鬼王合成面板标题解析（纯函数，无设备依赖，便于单测）。

合成面板的「标题星级」是**产物**，配置里的 `synthesis_list` 是**材料**星级，
两者相差一级：标题写「四星裂变之眼」时，消耗的是三星材料、产出四星鬼王。

实测（1280x720，合成详情页全屏帧 `md_adb_now.png`）：
    标题文字 bbox  x=311..455, y=156..181
    OCR roi        (305,148,165,40)  ← 对应 md/ocr.json 的 synthesis_level
    同帧 `I_MD_START_SYNTHESIS` 命中 1.000 @(906,562)，确认是同一套 UI

注意文案里**没有「合成」二字**，只有「四星裂变之眼」。因此正则不能带
`合成` 前缀，否则这版 UI 上永远解析失败。
"""

import re

# 标题里的大写中文数字 → 产物星级
_PRODUCT_LEVEL = {'二': 2, '三': 3, '四': 4, '五': 5, '六': 6}
_TITLE_PATTERN = re.compile(r'([二三四五六])星裂变之眼')


def parse_synthesis_material_level(text: str) -> int | None:
    """从合成面板标题反解出「材料星级」。

    Args:
        text: OCR 原文，可能带空格、换行或前后杂字。

    Returns:
        材料星级 1-5；无法解析返回 None。

    Note:
        返回 None 表示「没有证据」，调用方必须按未验证放行，**不能当校验失败**。
        OCR 会因弹窗位移、roi 偏移、动画残影而读空；若把 None 判成失败并
        重试 3 次，结果就是「一次都没合成就全被跳过」，比不校验更糟。

        一星鬼王的材料没有星级，标题不会出现「X星裂变之眼」，
        解析必然返回 None → 天然走放行分支，不会误伤。
    """
    if not text:
        return None
    cleaned = re.sub(r'\s+', '', str(text))
    matched = _TITLE_PATTERN.search(cleaned)
    if matched is None:
        return None
    return _PRODUCT_LEVEL[matched.group(1)] - 1
