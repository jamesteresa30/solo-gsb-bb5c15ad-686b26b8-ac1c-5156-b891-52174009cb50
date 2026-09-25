"""潮汐通航窗口核心计算（纯标准库，便于单测）。

规则：
- 实际水深(t) = 航道基准水深 + 潮高(t)；
- 相邻预报点之间潮高按线性插值；
- 只在预报覆盖时段 [首个预报点, 末个预报点] 内计算；
- 水深 >= 吃水 + 安全富余 的连续时段为达标窗口，端点恰好达标也纳入；
- 可出发时段 = [窗口起点, 窗口终点 − 通航时长]；窗口长度不足通航时长的舍弃。

预约水深复核（depth_deficit_audit）：
- 对已排定的通航区间按最新预报点线性插值复算水深；
- 区间内水深 < 所需水深 的连续时段合并为欠深段，并给出各段最大欠深；
- 预报首末点未覆盖到的通航时段单独作为“未覆盖时段”返回，
  不插值、不外推，也绝不能判为安全。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

_EPS = 1e-9  # 浮点容差，保证“恰好达标”的端点被纳入

ForecastPoint = tuple[datetime, float]  # (预报时刻, 潮高)


def _cross_time(t0: float, d0: float, t1: float, d1: float, required: float) -> float:
    """线性插值下水深恰好等于 required 的时刻（Unix 秒）。"""
    frac = (required - d0) / (d1 - d0)
    return t0 + frac * (t1 - t0)


def sufficient_depth_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
) -> list[tuple[float, float]]:
    """返回水深达标的连续窗口列表 [(start_ts, end_ts)]（Unix 秒，按时间升序）。

    points 需按时刻升序且时刻唯一；不足 2 个预报点时无插值区间，返回空。
    """
    if len(points) < 2:
        return []

    ts = [p[0].timestamp() for p in points]
    depths = [reference_depth + p[1] for p in points]

    windows: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    def flush() -> None:
        nonlocal cur_start, cur_end
        if cur_start is not None:
            windows.append((cur_start, cur_end))  # type: ignore[arg-type]
            cur_start = cur_end = None

    for i in range(len(points) - 1):
        t0, t1 = ts[i], ts[i + 1]
        d0, d1 = depths[i], depths[i + 1]
        above0 = d0 >= required_depth - _EPS
        above1 = d1 >= required_depth - _EPS

        if above0 and above1:
            seg = (t0, t1)
        elif not above0 and not above1:
            # 线性段两端都不达标，整段不达标；关闭当前窗口
            flush()
            continue
        elif above0:
            seg = (t0, _cross_time(t0, d0, t1, d1, required_depth))
        else:
            seg = (_cross_time(t0, d0, t1, d1, required_depth), t1)

        if cur_start is None:
            cur_start, cur_end = seg
        elif seg[0] <= cur_end + 1e-6:  # type: ignore[operator]
            # 相邻段首尾相接，合并为同一连续窗口
            cur_end = max(cur_end, seg[1])  # type: ignore[type-var]
        else:
            flush()
            cur_start, cur_end = seg

    flush()
    return windows


def departure_windows(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
    duration_seconds: float,
) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """计算全部可出发时段与最早出发时刻。

    返回 ([(start, end), ...], earliest)，时间均为 UTC datetime。
    """
    result: list[tuple[datetime, datetime]] = []
    for start_ts, end_ts in sufficient_depth_windows(points, reference_depth, required_depth):
        if end_ts - start_ts >= duration_seconds - _EPS:
            result.append((_to_utc(start_ts), _to_utc(end_ts - duration_seconds)))
    earliest = min((w[0] for w in result), default=None)
    return result, earliest


def earliest_scheduled_departure(
    departable_windows: list[tuple[datetime, datetime]],
    duration_seconds: float,
    allowed_start: datetime,
    allowed_end: datetime,
    blockers: list[tuple[datetime, datetime]],
) -> datetime | None:
    """在潮汐可出发时段内、请求允许范围内，安排不与已有预约重叠的最早出发时刻。

    - departable_windows：departure_windows 求出的可出发时段 [s, e]
      （出发时刻 d ∈ [s, e] 即可保证整个通航过程 [d, d+时长] 水深达标）；
    - allowed_start/end：请求方允许的出发时刻范围（含端点）；
    - blockers：该港口已有有效预约的通航占用区间 [占用起点, 占用终点]；
      首尾相接允许（新区间端点恰好贴住已有区间不算重叠）。

    返回最早可出发时刻（UTC datetime）；无可用时刻返回 None。
    """
    range_lo, range_hi = allowed_start.timestamp(), allowed_end.timestamp()

    # 候选出发区间 = 潮汐可出发时段 ∩ 请求允许范围
    candidates: list[tuple[float, float]] = []
    for start, end in departable_windows:
        lo = max(start.timestamp(), range_lo)
        hi = min(end.timestamp(), range_hi)
        if lo <= hi:
            candidates.append((lo, hi))

    # 已有占用 [a, b] 会禁止 d ∈ (a-时长, b)：d == a-时长（首尾相接）
    # 与 d == b（紧随其后）均允许，因此从候选区间中挖掉开区间、保留两个端点。
    for occ_start, occ_end in sorted(blockers, key=lambda x: x[0].timestamp()):
        forbidden_lo = occ_start.timestamp() - duration_seconds
        forbidden_hi = occ_end.timestamp()
        remaining: list[tuple[float, float]] = []
        for lo, hi in candidates:
            if lo <= forbidden_lo:  # 左侧 [lo, forbidden_lo] 可出发（含相接端点）
                remaining.append((lo, min(hi, forbidden_lo)))
            if hi >= forbidden_hi:  # 右侧 [forbidden_hi, hi] 可出发（含相接端点）
                remaining.append((max(lo, forbidden_hi), hi))
        candidates = remaining
        if not candidates:
            return None

    if not candidates:
        return None
    return _to_utc(min(lo for lo, _ in candidates))


def _to_utc(ts: float) -> datetime:
    """Unix 秒转 UTC datetime，四舍五入到整秒避免浮点噪声。"""
    return datetime.fromtimestamp(round(ts), tz=timezone.utc)


# ---------------------------------------------------------------------------
# 预约水深复核
# ---------------------------------------------------------------------------

@dataclass
class DepthAudit:
    """通航区间水深复核结果（时间均为 Unix 秒）。

    - deficits：水深严格低于所需水深的连续时段 [(start, end, max_shortfall)]，
      max_shortfall = 该段内（所需水深 − 实际水深）的最大值（米）；
    - uncovered：预报首末点未覆盖到的通航时段 [(start, end)]，
      这些时段无法复算水深，不代表安全。
    """

    deficits: list[tuple[float, float, float]]
    uncovered: list[tuple[float, float]]

    @property
    def fully_covered(self) -> bool:
        return not self.uncovered

    @property
    def safe(self) -> bool:
        """全覆盖且无欠深段才可判为安全。"""
        return self.fully_covered and not self.deficits


def _interpolate_depth(
    ts: list[float], depths: list[float], t: float
) -> float:
    """相邻预报点间线性插值求 t 处水深；t 恰在预报点上时取该点值。"""
    for i in range(len(ts) - 1):
        if ts[i] - _EPS <= t <= ts[i + 1] + _EPS:
            if t <= ts[i] + _EPS:
                return depths[i]
            if t >= ts[i + 1] - _EPS:
                return depths[i + 1]
            frac = (t - ts[i]) / (ts[i + 1] - ts[i])
            return depths[i] + frac * (depths[i + 1] - depths[i])
    raise ValueError("t 超出预报覆盖范围")  # pragma: no cover - 调用方保证


def depth_deficit_audit(
    points: list[ForecastPoint],
    reference_depth: float,
    required_depth: float,
    interval_start: datetime,
    interval_end: datetime,
) -> DepthAudit:
    """复核已排定通航区间 [interval_start, interval_end] 的水深。

    实际水深 = reference_depth + 潮高，相邻预报点间线性插值。仅在预报覆盖时段
    [首点, 末点] 内可计算；通航区间落在覆盖范围外的部分作为未覆盖时段返回。
    水深 < required_depth 的连续时段合并为欠深段（恰等于所需水深不算欠深）。
    """
    start_ts, end_ts = interval_start.timestamp(), interval_end.timestamp()

    # 预报点不足 2 个：没有任何可插值区间，整个通航时段均未覆盖
    if len(points) < 2:
        return DepthAudit(deficits=[], uncovered=[(start_ts, end_ts)])

    ts = [p[0].timestamp() for p in points]
    depths = [reference_depth + p[1] for p in points]
    cov_start, cov_end = ts[0], ts[-1]

    # 预报覆盖范围与通航区间的交集（含端点相接）；其余为未覆盖时段
    covered_lo, covered_hi = max(start_ts, cov_start), min(end_ts, cov_end)
    uncovered: list[tuple[float, float]] = []
    if start_ts < cov_start - _EPS:
        uncovered.append((start_ts, min(end_ts, cov_start)))
    if end_ts > cov_end + _EPS:
        uncovered.append((max(start_ts, cov_end), end_ts))

    deficits: list[tuple[float, float, float]] = []
    if covered_lo <= covered_hi + _EPS:
        cur_start: float | None = None
        cur_end: float | None = None
        cur_shortfall: float = 0.0

        def flush() -> None:
            nonlocal cur_start, cur_end, cur_shortfall
            if cur_start is not None and cur_end - cur_start > _EPS:
                deficits.append((cur_start, cur_end, cur_shortfall))  # type: ignore[arg-type]
            cur_start = cur_end = None
            cur_shortfall = 0.0

        # 逐段处理与覆盖交集相交的每个插值区间
        for i in range(len(points) - 1):
            seg_lo = max(ts[i], covered_lo)
            seg_hi = min(ts[i + 1], covered_hi)
            if seg_lo > seg_hi + _EPS:
                continue
            d_lo = _interpolate_depth(ts, depths, seg_lo)
            d_hi = _interpolate_depth(ts, depths, seg_hi)
            below_lo = d_lo < required_depth - _EPS
            below_hi = d_hi < required_depth - _EPS

            if not below_lo and not below_hi:
                # 整段达标（线性段两端都不欠深则全程不欠深）：关闭当前欠深段
                flush()
                continue

            if below_lo and below_hi:
                low_lo, low_hi = seg_lo, seg_hi
            elif below_lo:
                # 由欠深升到达标：欠深到交点为止（交点恰好达标，归入安全侧）
                cross = _cross_time(ts[i], depths[i], ts[i + 1], depths[i + 1], required_depth)
                low_lo, low_hi = seg_lo, min(seg_hi, cross)
            else:
                cross = _cross_time(ts[i], depths[i], ts[i + 1], depths[i + 1], required_depth)
                low_lo, low_hi = max(seg_lo, cross), seg_hi

            if low_hi - low_lo <= _EPS:
                continue
            # 线性段内欠深量（required − depth）也是线性的，最大欠深在端点
            shortfall = max(
                0.0,
                required_depth - _interpolate_depth(ts, depths, low_lo),
                required_depth - _interpolate_depth(ts, depths, low_hi),
            )
            if cur_start is None:
                cur_start, cur_end, cur_shortfall = low_lo, low_hi, shortfall
            elif low_lo <= cur_end + 1e-6:  # type: ignore[operator]
                # 相邻欠深段首尾相接，合并并取全段最大欠深
                cur_end = max(cur_end, low_hi)  # type: ignore[type-var]
                cur_shortfall = max(cur_shortfall, shortfall)
            else:
                flush()
                cur_start, cur_end, cur_shortfall = low_lo, low_hi, shortfall

        flush()

    return DepthAudit(deficits=deficits, uncovered=uncovered)
