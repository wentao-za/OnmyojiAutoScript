# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
"""任务每日时间段：把排定的下次运行时间限制在用户设定的窗口内。

约定：
- 起止时间相同（含默认 00:00:00 / 00:00:00）表示全天不限制，原样返回。
- 起点小于终点为当日窗口 [start, end)；起点大于终点为跨日窗口，例如 22:00:00-06:00:00。
- 只向后顺延（口径 A），绝不早于调用方算出的时间。
"""
from datetime import datetime, time, timedelta

from module.logger import logger


def in_window(dt: datetime, start: time, end: time) -> bool:
    """判断 dt 是否落在每日窗口内。起止相同视为全天。"""
    if start == end:
        return True
    current = dt.time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


def next_open_time(dt: datetime, start: time, end: time) -> datetime:
    """返回不早于 dt 的最早窗口内时刻；dt 已在窗口内则原样返回。"""
    if in_window(dt, start, end):
        return dt
    candidate = datetime.combine(dt.date(), start)
    if candidate <= dt:
        # 今天的起点已过，只能是次日
        candidate = datetime.combine(dt.date() + timedelta(days=1), start)
    return candidate.replace(microsecond=0)


def apply_time_window(task: str, scheduler, next_run: datetime, forced: bool = False) -> datetime:
    """把 next_run 限制在该任务的每日时间段内。

    :param task: 任务名，仅用于日志
    :param scheduler: 含 time_window_start / time_window_end 的 Scheduler 对象
    :param next_run: 已算出的下次运行时间
    :param forced: 该时间是否来自强制时间点（显式 target 或 server_update），仅决定日志级别
    """
    start = scheduler.time_window_start
    end = scheduler.time_window_end
    if start == end:
        # 全天不限制：零开销直接返回
        return next_run

    current = next_run.replace(microsecond=0)
    if in_window(current, start, end):
        # 已在窗口内，含“已过期且窗口内”的情形：保持立即执行语义
        return next_run

    now = datetime.now().replace(microsecond=0)
    # 过去的时间不会被框架自动顺延，这里以 now 为下界重新找最近的窗口起点
    target = next_open_time(max(current, now), start, end)

    log = logger.warning if forced else logger.info
    log(f'任务时间段调整 {task}.scheduler.next_run: '
        f'{current:%Y-%m-%d %H:%M:%S} -> {target:%Y-%m-%d %H:%M:%S}, '
        f'窗口 {start:%H:%M:%S}-{end:%H:%M:%S}')
    return target
