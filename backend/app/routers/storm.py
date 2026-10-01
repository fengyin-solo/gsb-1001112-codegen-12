"""风雪事件指挥板接口。

路由层只做参数收发，所有业务判断（状态机、连续窗口、事务一致、防汛冲突、
幂等续传）都在 app.services.storm 里。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.storm import STATUS_ORDER, BusinessError, storm_service

router = APIRouter(prefix="/api/storm", tags=["风雪事件指挥板"])


class OpPayload(BaseModel):
    """带操作号的写请求：断网排队复位后靠操作号幂等续传。"""

    values: dict[str, Any] = Field(default_factory=dict)
    op_no: str | None = None


class SyncPayload(BaseModel):
    operations: list[dict[str, Any]] = Field(default_factory=list)


def _run(call):
    """把业务异常统一翻译成 ok=False 的动作结果，和其它模块的动作口径保持一致。"""
    try:
        return call()
    except BusinessError as exc:
        return {"ok": False, "message": str(exc)}


@router.get("/board")
def board(
    status: str | None = Query(default=None, description="待启动、执行中、收尾、已关闭"),
    keyword: str | None = Query(default=None, description="按事件编号或名称检索"),
) -> dict[str, Any]:
    """指挥板聚合视图：事件、连续窗口、车辆位置、材料库存、在办防汛占用段。"""
    if status and status not in STATUS_ORDER:
        raise HTTPException(status_code=400, detail=f"事件状态只支持：{'、'.join(STATUS_ORDER)}")
    storm_service.ensure_seed()
    return storm_service.board(status=status, keyword=keyword)


@router.get("/events/{event_id}")
def event_detail(event_id: int) -> dict[str, Any]:
    """事件详情与三处台账（除雪作业台账、路段阶段、物资清单），全部引用同一事件。"""
    try:
        return storm_service.event_detail(event_id)
    except BusinessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/events")
def create_event(payload: OpPayload) -> dict[str, Any]:
    """登记风雪事件，并把气象时段与作业路段编排成连续事件窗口。"""
    return _run(lambda: storm_service.create_event(payload.values, op_no=payload.op_no or ""))


@router.post("/events/{event_id}/transition")
def transition(event_id: int, payload: OpPayload) -> dict[str, Any]:
    """事件状态推进：待启动→执行中→收尾→已关闭，只能顺序前进，不可回退重开。"""
    action = str(payload.values.get("action") or "").strip()
    return _run(lambda: storm_service.transition(event_id, action, op_no=payload.op_no or ""))


@router.post("/dispatches")
def dispatch(payload: OpPayload) -> dict[str, Any]:
    """调度会话下结论：任务派发、车辆占用、材料扣减事务相符，三处台账同引事件。"""
    return _run(lambda: storm_service.dispatch(payload.values, op_no=payload.op_no or ""))


@router.post("/positions")
def report_position(payload: OpPayload) -> dict[str, Any]:
    """上报车辆实时位置，挂接到对应事件窗口的时间线。"""
    return _run(lambda: storm_service.report_position(payload.values, op_no=payload.op_no or ""))


@router.get("/oplog")
def list_ops(event_id: int | None = Query(default=None, description="可选：只看某事件的操作号")) -> dict[str, Any]:
    """操作号流水：用于核对断网续传的幂等命中情况。"""
    items = storm_service.list_ops(event_id)
    return {"items": items, "total": len(items)}


@router.post("/sync")
def sync_batch(payload: SyncPayload) -> dict[str, Any]:
    """断网复位后续传：按操作号幂等回放排队操作，重复操作号不会二次扣减。"""
    return storm_service.sync_batch(payload.operations)
