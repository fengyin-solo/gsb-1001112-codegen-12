"""风雪事件指挥板接口。

路由层只做取参与转发：状态机、事务、指挥令裁决、幂等等业务判断全部在
app/services/snow_command.py。写接口统一接受「操作号」用于断网幂等续传。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.snow_command import service

router = APIRouter(prefix="/api/snow", tags=["风雪事件指挥板"])


class ValuesPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SyncPayload(BaseModel):
    operations: list[dict[str, Any]] = Field(default_factory=list)


def _body(payload: ValuesPayload) -> dict[str, Any]:
    """动作接口兼容 {values:{...}} 与扁平字段两种提交方式。"""
    values = payload.values or {}
    values.setdefault("操作号", values.get("操作号"))
    return values


@router.get("/events")
def list_events(status: str | None = None, keyword: str | None = None) -> dict[str, Any]:
    items = service.list_events(status=status, keyword=keyword)
    return {"items": items, "total": len(items)}


@router.post("/events")
def create_event(payload: ValuesPayload) -> dict[str, Any]:
    """建立风雪事件：气象时段、天气基准快照、初始封控段与物资清单一并编排。"""
    values = payload.values or {}
    return service.create_event(values, str(values.get("操作号") or ""), "/api/snow/events")


@router.get("/events/{event_id}")
def get_board(event_id: int) -> dict[str, Any]:
    """指挥板主数据：事件、封控段（含车辆位置）、连续窗口、台账、物资、现行指挥令。"""
    board, message = service.board(event_id)
    if board is None:
        return {"ok": False, "message": message}
    return {"ok": True, **board}


@router.post("/events/{event_id}/actions")
def event_action(event_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """事件状态流转：启动事件 / 转入收尾 / 关闭事件，严格顺序。"""
    values = _body(payload)
    return service.event_action(
        event_id, str(values.get("action") or ""),
        str(values.get("操作号") or ""), f"/api/snow/events/{event_id}/actions", values,
    )


@router.post("/events/{event_id}/segments/{segment_id}/actions")
def segment_action(event_id: int, segment_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """封控段阶段流转，可附带调度会话结论写入路段阶段。"""
    values = _body(payload)
    return service.segment_action(
        event_id, segment_id, str(values.get("action") or ""),
        str(values.get("操作号") or ""),
        f"/api/snow/events/{event_id}/segments/{segment_id}/actions", values,
    )


@router.post("/events/{event_id}/dispatches")
def dispatch(event_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """从封控段直接调用车辆、班组与材料；事务内完成派发、占用、扣减。"""
    values = _body(payload)
    return service.dispatch(
        event_id, values, str(values.get("操作号") or ""),
        f"/api/snow/events/{event_id}/dispatches",
    )


@router.post("/events/{event_id}/tasks/{task_id}/actions")
def task_action(event_id: int, task_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """调度任务：开始作业 / 完成任务；完成时释放车辆并回填台账结论。"""
    values = _body(payload)
    return service.task_action(
        event_id, task_id, str(values.get("action") or ""), values,
        str(values.get("操作号") or ""),
        f"/api/snow/events/{event_id}/tasks/{task_id}/actions",
    )


@router.post("/events/{event_id}/conclusions")
def add_conclusion(event_id: int, payload: ValuesPayload) -> dict[str, Any]:
    """调度会话结论一次写入台账、路段阶段、物资清单三处（同一事件引用）。"""
    values = _body(payload)
    return service.add_conclusion(
        event_id, values, str(values.get("操作号") or ""),
        f"/api/snow/events/{event_id}/conclusions",
    )


@router.get("/events/{event_id}/references")
def references(event_id: int) -> dict[str, Any]:
    """核对三处台账是否都引用同一事件。"""
    return service.references(event_id)


@router.get("/vehicles")
def vehicle_positions() -> dict[str, Any]:
    """车辆位置板：在库/占用状态与所在封控段。"""
    items = service.vehicle_positions()
    return {"items": items, "total": len(items)}


@router.get("/orders")
def list_orders() -> dict[str, Any]:
    items = service.list_orders()
    return {"items": items, "total": len(items)}


@router.post("/orders")
def create_order(payload: ValuesPayload) -> dict[str, Any]:
    """下达应急指挥令：除雪与防汛同时占用道路时据此确定优先方。"""
    values = payload.values or {}
    return service.create_order(values, str(values.get("操作号") or ""), "/api/snow/orders")


@router.post("/orders/{order_id}/revoke")
def revoke_order(order_id: int, payload: ValuesPayload) -> dict[str, Any]:
    values = _body(payload)
    return service.revoke_order(
        order_id, str(values.get("操作号") or ""),
        f"/api/snow/orders/{order_id}/revoke", values,
    )


@router.post("/sync")
def sync(payload: SyncPayload) -> dict[str, Any]:
    """断网复位续传：按操作号逐笔幂等重放，重复操作不重复扣料。"""
    return service.sync(payload.operations)
