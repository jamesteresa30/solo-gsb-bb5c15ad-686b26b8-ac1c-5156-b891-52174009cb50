"""Pydantic 请求/响应模型。"""
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer, model_validator

# 响应中 UTC 时刻统一序列化为带显式偏移的 ISO 8601（...+00:00），
# 不随 pydantic 版本改用 “Z” 后缀，保证文档与响应格式稳定。
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(
        lambda dt: dt.astimezone(timezone.utc).isoformat() if dt is not None else None,
        return_type=str,
        when_used="json",
    ),
]


class PortCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="港口名称")
    reference_depth: float = Field(description="航道基准水深（米）")


class PortUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    reference_depth: float | None = Field(default=None, description="航道基准水深（米）")


class PortOut(BaseModel):
    id: int
    name: str
    reference_depth: float


class TideForecastPoint(BaseModel):
    forecast_time: datetime = Field(description="预报时刻（ISO 8601；无时区信息按 UTC 处理）")
    tide_height: float = Field(description="潮高（米，相对海图基准面）")


class TideForecastUpsert(BaseModel):
    """批量录入/更正预报点：同一港口同一时刻重复提交即覆盖（更正）。"""

    points: list[TideForecastPoint] = Field(min_length=1)


class TideForecastOut(BaseModel):
    forecast_time: UtcDatetime
    tide_height: float


class WindowQuery(BaseModel):
    draft: float = Field(ge=0, description="船舶吃水（米）")
    safety_margin: float = Field(ge=0, description="安全富余（米）")
    duration_minutes: float = Field(gt=0, description="所需通航时长（分钟），必须为正数")


class DepartureWindow(BaseModel):
    start: UtcDatetime = Field(description="可出发时段起点")
    end: UtcDatetime = Field(description="可出发时段终点（达标窗口结束时刻 − 通航时长）")


class WindowQueryResult(BaseModel):
    port_id: int
    required_depth: float = Field(description="所需水深 = 吃水 + 安全富余（米）")
    duration_minutes: float
    earliest_departure: UtcDatetime | None = Field(
        description="最早可出发时刻；无可出发时段时为 null"
    )
    windows: list[DepartureWindow] = Field(description="全部可出发时段")


class ReservationCreate(BaseModel):
    ship_name: str = Field(min_length=1, max_length=100, description="船名")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    safety_margin: float = Field(ge=0, description="安全富余（米）")
    duration_minutes: float = Field(gt=0, description="所需通航时长（分钟），必须为正数")
    allowed_departure_from: datetime = Field(description="允许出发范围起点（ISO 8601；无时区信息按 UTC）")
    allowed_departure_to: datetime = Field(description="允许出发范围终点（ISO 8601；无时区信息按 UTC）")

    @model_validator(mode="after")
    def _validate_range(self):
        start = self.allowed_departure_from
        end = self.allowed_departure_to
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start > end:
            raise ValueError("allowed_departure_from 不得晚于 allowed_departure_to")
        return self


class ReservationOut(BaseModel):
    id: int
    port_id: int
    ship_name: str
    draft: float
    safety_margin: float
    duration_minutes: float
    allowed_departure_from: UtcDatetime = Field(description="请求的允许出发范围起点")
    allowed_departure_to: UtcDatetime = Field(description="请求的允许出发范围终点")
    departure_start: UtcDatetime = Field(description="排定的出发时刻")
    departure_end: UtcDatetime = Field(description="排定的通航结束时刻 = 出发时刻 + 通航时长")
    status: str = Field(description="状态：active / cancelled")
    created_at: UtcDatetime
    canceled_at: UtcDatetime | None = None


class DepthDeficitSegment(BaseModel):
    """通航区间内水深低于所需水深的一段连续时段。"""

    start: UtcDatetime = Field(description="欠深时段起点")
    end: UtcDatetime = Field(description="欠深时段终点")
    max_shortfall: float = Field(
        description="该段最大欠深（米）= 所需水深 − 实际水深 的最大值；> 0"
    )


class UncoveredInterval(BaseModel):
    """通航区间内未被预报首末点覆盖、无法复算水深的时段。"""

    start: UtcDatetime = Field(description="未覆盖时段起点")
    end: UtcDatetime = Field(description="未覆盖时段终点")


class DepthAuditResult(BaseModel):
    """预约水深复核结果（GET /reservations/{id}/depth-audit）。

    仅反映查询时刻数据库中的最新预报，为只读复核：不改动预约与原排程。
    safe 仅在预报完整覆盖整个通航区间且无欠深段时为 true；
    存在未覆盖时段时即使已覆盖部分水深达标也不能判为安全。
    """

    reservation_id: int
    port_id: int
    reference_depth: float = Field(description="该港口航道基准水深（米）")
    required_depth: float = Field(description="所需水深 = 吃水 + 安全富余（米）")
    transit_start: UtcDatetime = Field(description="复核的通航区间起点（预约排定的出发时刻）")
    transit_end: UtcDatetime = Field(description="复核的通航区间终点（预约排定的通航结束时刻）")
    forecast_point_count: int = Field(description="查询时该港口最新预报点数量")
    fully_covered: bool = Field(description="预报是否完整覆盖整个通航区间")
    safe: bool = Field(description="全覆盖且无欠深段才为 true；覆盖不足恒为 false")
    deficit_segments: list[DepthDeficitSegment] = Field(
        description="水深低于所需水深的连续时段（已合并），按起点升序"
    )
    uncovered_intervals: list[UncoveredInterval] = Field(
        description="预报未覆盖的通航时段（无法复算水深，不能视为安全），按起点升序"
    )
    audited_at: UtcDatetime = Field(description="复核执行时刻（UTC），结果仅代表该时刻的预报")
