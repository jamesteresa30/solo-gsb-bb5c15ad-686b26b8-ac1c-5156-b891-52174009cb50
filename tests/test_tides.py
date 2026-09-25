"""核心窗口计算算法的单元测试（仅标准库）。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.tides import (
    departure_windows,
    depth_deficit_audit,
    earliest_scheduled_departure,
    sufficient_depth_windows,
)

BASE = datetime(2026, 9, 24, tzinfo=timezone.utc)


def pts(*heights: float, step_minutes: int = 60):
    """从 BASE 起按等间隔生成 (时刻, 潮高) 序列。"""
    return [
        (BASE + timedelta(minutes=step_minutes * i), h) for i, h in enumerate(heights)
    ]


def test_single_tide_cycle_window():
    # 基准水深 3，潮高 1→3→1：水深 4→6→4，要求 5
    # 上升段 00:30 达标，下降段 01:30 退出
    windows = sufficient_depth_windows(pts(1, 3, 1), reference_depth=3.0, required_depth=5.0)
    assert len(windows) == 1
    start, end = windows[0]
    assert start == (BASE + timedelta(minutes=30)).timestamp()
    assert end == (BASE + timedelta(hours=1, minutes=30)).timestamp()


def test_exact_threshold_endpoints_included():
    # 水深恰好全程等于要求值，端点恰好达标也纳入
    windows = sufficient_depth_windows(pts(2, 2), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=1)).timestamp())]


def test_departure_window_subtracts_duration():
    # 达标窗口 [00:30, 01:30]，通航 30 分钟 → 可出发 [00:30, 01:00]
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=1800
    )
    assert len(deps) == 1
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1)
    assert earliest == BASE + timedelta(minutes=30)


def test_window_shorter_than_duration_discarded():
    # 达标窗口 60 分钟 < 所需 90 分钟 → 舍弃
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=5400
    )
    assert deps == []
    assert earliest is None


def test_duration_exactly_equals_window_kept():
    # 窗口 60 分钟 == 所需 60 分钟 → 保留，可出发时段退化为同一时刻
    deps, earliest = departure_windows(
        pts(1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=3600
    )
    assert len(deps) == 1
    assert deps[0][0] == deps[0][1] == BASE + timedelta(minutes=30)
    assert earliest == BASE + timedelta(minutes=30)


def test_multiple_windows():
    # 潮高 1,3,1,3,1（基准 3，要求 5）：两个独立高潮窗口 [00:30,01:30]、[02:30,03:30]
    deps, earliest = departure_windows(
        pts(1, 3, 1, 3, 1), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert len(deps) == 2
    # 第一个窗口 [00:30, 01:30]，可出发 [00:30, 01:29]
    assert deps[0][0] == BASE + timedelta(minutes=30)
    assert deps[0][1] == BASE + timedelta(hours=1, minutes=29)
    # 第二个窗口 [02:30, 03:30]
    assert deps[1][0] == BASE + timedelta(hours=2, minutes=30)
    assert deps[1][1] == BASE + timedelta(hours=3, minutes=29)
    assert earliest == deps[0][0]


def test_all_below_required():
    deps, earliest = departure_windows(
        pts(0.5, 1.0, 0.5), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_fewer_than_two_points_no_coverage():
    deps, earliest = departure_windows(
        pts(5.0), reference_depth=3.0, required_depth=5.0, duration_seconds=60
    )
    assert deps == []
    assert earliest is None


def test_window_clipped_at_forecast_coverage():
    # 全程达标时窗口就是预报覆盖时段本身，不外推
    windows = sufficient_depth_windows(pts(3, 4, 3), reference_depth=3.0, required_depth=5.0)
    assert windows == [(BASE.timestamp(), (BASE + timedelta(hours=2)).timestamp())]


# ---------------------------------------------------------------------------
# earliest_scheduled_departure：潮汐窗口 + 请求范围 + 已有预约排程
# ---------------------------------------------------------------------------

# 一个 [00:00, 04:00] 的可出发时段（出发时刻落在此区间内即可）
DEP_WINDOWS = [(BASE, BASE + timedelta(hours=4))]
DURATION = 3600  # 60 分钟


def test_schedule_earliest_within_range():
    # 无占用、范围覆盖整个窗口 → 取范围起点（最早）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=3), [],
    )
    assert dep == BASE


def test_schedule_intersects_range_start():
    # 允许范围从 01:00 开始 → 01:00 出发
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=1), BASE + timedelta(hours=3), [],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_blocker_pushes_after_end():
    # 已有预约占用 [00:30, 01:30] → d 必须 >= 01:30（首尾相接允许）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE + timedelta(minutes=30), BASE + timedelta(hours=1, minutes=30))],
    )
    assert dep == BASE + timedelta(hours=1, minutes=30)


def test_schedule_departure_exactly_touching_blocker_start_allowed():
    # 预约占用 [01:00, 02:00]：00:00 出发、01:00 结束，首尾相接 → 允许
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE + timedelta(hours=1), BASE + timedelta(hours=2))],
    )
    assert dep == BASE


def test_schedule_blocker_covering_range_start_takes_right_edge():
    # 占用 [00:00, 01:00]：00:00 出发会在 [00:00,01:00] 内重叠 → 最早 01:00
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [(BASE, BASE + timedelta(hours=1))],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_no_room_between_blockers():
    # 两个相邻占用 [00:00,01:00]、[01:30,02:30]：
    # 间隙 01:00 出发需通航到 02:00，与第二个预约重叠 → 只能 02:30
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [
            (BASE, BASE + timedelta(hours=1)),
            (BASE + timedelta(hours=1, minutes=30), BASE + timedelta(hours=2, minutes=30)),
        ],
    )
    assert dep == BASE + timedelta(hours=2, minutes=30)


def test_schedule_exact_fit_between_blockers():
    # 占用 [00:00,01:00]、[02:00,03:00]：间隙恰好 1 小时，01:00 出发 02:00 结束
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=4),
        [
            (BASE, BASE + timedelta(hours=1)),
            (BASE + timedelta(hours=2), BASE + timedelta(hours=3)),
        ],
    )
    assert dep == BASE + timedelta(hours=1)


def test_schedule_range_fully_blocked_returns_none():
    # 占用 [00:00, 03:30]，允许范围 00:00–02:30 内无可行出发
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE, BASE + timedelta(hours=2, minutes=30),
        [(BASE, BASE + timedelta(hours=3, minutes=30))],
    )
    assert dep is None


def test_schedule_range_outside_tide_windows_returns_none():
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=5), BASE + timedelta(hours=6), [],
    )
    assert dep is None


def test_schedule_range_touching_tide_window_endpoint_allowed():
    # 可出发时段终点 04:00，允许范围末端恰好 04:00 → 04:00 出发（端点纳入）
    dep = earliest_scheduled_departure(
        DEP_WINDOWS, DURATION,
        BASE + timedelta(hours=4), BASE + timedelta(hours=4),
        [],
    )
    assert dep == BASE + timedelta(hours=4)


# ---------------------------------------------------------------------------
# depth_deficit_audit：预约水深复核
# ---------------------------------------------------------------------------

def _audit(heights, start, end, reference_depth=3.0, required=5.0, step_minutes=60):
    return depth_deficit_audit(
        pts(*heights, step_minutes=step_minutes),
        reference_depth=reference_depth,
        required_depth=required,
        interval_start=BASE + timedelta(minutes=start),
        interval_end=BASE + timedelta(minutes=end),
    )


def test_audit_safe_whole_covered_interval():
    # 水深 4→6→4，要求 5；复核 [00:30, 01:30] 恰好是达标窗口 → 安全
    a = _audit((1, 3, 1), 30, 90)
    assert a.fully_covered is True
    assert a.deficits == []
    assert a.uncovered == []
    assert a.safe is True


def test_audit_deficit_at_start_with_max_shortfall():
    # 水深 4→6→4，要求 5；复核 [00:00, 01:00]：
    # [00:00, 00:30] 欠深，最大欠深在 00:00：5 − 4 = 1
    a = _audit((1, 3, 1), 0, 60)
    assert a.fully_covered is True
    assert len(a.deficits) == 1
    s, e, shortfall = a.deficits[0]
    assert s == BASE.timestamp()
    assert e == (BASE + timedelta(minutes=30)).timestamp()
    assert shortfall == pytest.approx(1.0)
    assert a.safe is False


def test_audit_merges_touching_deficit_segments():
    # 潮高 1,3,1,3,1（水深 4,6,4,6,4），要求 5：两个欠深段 [01:30,02:30] 与两侧
    # 复核 [00:30, 03:30]：[01:30,02:30] 为欠深（谷值水深 4，最大欠深 1）
    a = _audit((1, 3, 1, 3, 1), 30, 210)
    assert a.uncovered == []
    assert len(a.deficits) == 1
    s, e, shortfall = a.deficits[0]
    assert s == (BASE + timedelta(hours=1, minutes=30)).timestamp()
    assert e == (BASE + timedelta(hours=2, minutes=30)).timestamp()
    assert shortfall == pytest.approx(1.0)


def test_audit_multiple_separate_deficits():
    # 复核 [00:00, 04:00]：欠深段 [00:00,00:30] 与 [01:30,02:30] 与 [03:30,04:00]
    a = _audit((1, 3, 1, 3, 1), 0, 240)
    assert len(a.deficits) == 3
    bounds = [
        (BASE + timedelta(minutes=0), BASE + timedelta(minutes=30)),
        (BASE + timedelta(hours=1, minutes=30), BASE + timedelta(hours=2, minutes=30)),
        (BASE + timedelta(hours=3, minutes=30), BASE + timedelta(hours=4)),
    ]
    for (s, e, shortfall), (bs, be) in zip(a.deficits, bounds):
        assert s == bs.timestamp()
        assert e == be.timestamp()
        assert shortfall == pytest.approx(1.0)
    assert a.safe is False


def test_audit_exact_threshold_not_a_deficit():
    # 水深全程恰好等于要求值 → 不算欠深
    a = _audit((2, 2), 0, 60, required=5.0)
    assert a.deficits == []
    assert a.safe is True


def test_audit_interval_ends_before_forecast_start_uncovered():
    # 通航区间 [−2h, −1h] 完全在预报首点（00:00）之前 → 整段未覆盖，不能判安全
    a = _audit((1, 3, 1), -120, -60)
    assert a.fully_covered is False
    assert a.safe is False
    assert a.deficits == []
    assert a.uncovered == [
        ((BASE - timedelta(hours=2)).timestamp(), (BASE - timedelta(hours=1)).timestamp())
    ]


def test_audit_interval_partial_uncovered_before_and_after():
    # 预报覆盖 [00:00, 02:00]，通航 [−1h, 03:00]：
    # 未覆盖 [−1h,00:00]、[02:00,03:00]；中间 [00:00,02:00] 水深 4→6→4
    # 欠深 [00:00,00:30] 与 [01:30,02:00]
    a = _audit((1, 3, 1), -60, 180)
    assert a.fully_covered is False
    assert a.safe is False
    assert a.uncovered == [
        ((BASE - timedelta(hours=1)).timestamp(), BASE.timestamp()),
        ((BASE + timedelta(hours=2)).timestamp(), (BASE + timedelta(hours=3)).timestamp()),
    ]
    assert len(a.deficits) == 2


def test_audit_uncovered_even_when_covered_part_is_safe():
    # 已覆盖部分全程达标，但尾部超出预报末点 → 仍不能判安全
    a = _audit((3, 4, 3), 30, 150)  # 覆盖到 02:00，通航到 02:30
    assert a.deficits == []
    assert a.fully_covered is False
    assert a.safe is False
    assert a.uncovered == [
        ((BASE + timedelta(hours=2)).timestamp(), (BASE + timedelta(hours=2, minutes=30)).timestamp())
    ]


def test_audit_fewer_than_two_points_entire_interval_uncovered():
    a = depth_deficit_audit(
        pts(5.0),
        reference_depth=3.0,
        required_depth=5.0,
        interval_start=BASE,
        interval_end=BASE + timedelta(hours=1),
    )
    assert a.deficits == []
    assert a.fully_covered is False
    assert a.safe is False
    assert a.uncovered == [
        (BASE.timestamp(), (BASE + timedelta(hours=1)).timestamp())
    ]


def test_audit_coverage_boundary_touching_counts_as_covered():
    # 通航区间恰好等于预报覆盖范围 [00:00, 02:00] → 完全覆盖
    a = _audit((1, 3, 1), 0, 120)
    assert a.fully_covered is True
    assert a.uncovered == []
