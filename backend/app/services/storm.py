"""风雪事件指挥板业务规则。

围绕一次风雪过程把气象时段、作业路段、车辆位置编排成连续的事件窗口，
并在调度会话落结论时把同一事件号同时写进除雪作业台账、路段阶段和物资清单。

关键约束都收在本模块：
- 事件状态只能按 待启动 → 执行中 → 收尾 → 已关闭 顺序前进，不能跳步、不能回退、不能重开；
- 事件窗口必须连续铺满整个气象时段，路段不允许在同一时间重叠；
- 任务派发、车辆占用、材料扣减在同一事务里提交，任一步校验不过整体回滚；
- 除雪与防汛同时占用道路时，必须凭应急指挥令确定优先级后才能派发；
- 历史作业按事件当时的天气基准冻结保留，关闭后任何写操作都被拒绝；
- 所有写操作带操作号，重放同一操作号直接返回首次结果，保证断网续传幂等。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "storm_event"
WINDOW_MODULE = "storm_window"
DISPATCH_MODULE = "storm_dispatch"
ROAD_PHASE_MODULE = "storm_road_phase"
MATERIAL_ITEM_MODULE = "storm_material_item"
VEHICLE_OCC_MODULE = "storm_vehicle_occ"
POSITION_MODULE = "storm_vehicle_position"
OPLOG_MODULE = "storm_oplog"

# 事件状态机：只能按下标顺序前进一格。
STATUS_PENDING = "待启动"
STATUS_RUNNING = "执行中"
STATUS_WRAPUP = "收尾"
STATUS_CLOSED = "已关闭"
STATUS_ORDER = [STATUS_PENDING, STATUS_RUNNING, STATUS_WRAPUP, STATUS_CLOSED]
NEXT_ACTION = {
    STATUS_PENDING: ("启动事件", STATUS_RUNNING),
    STATUS_RUNNING: ("转入收尾", STATUS_WRAPUP),
    STATUS_WRAPUP: ("关闭事件", STATUS_CLOSED),
}
# 允许通过通用动作接口传入的动作 → 目标状态（只允许相邻推进）。
TRANSITIONS = {
    "启动事件": STATUS_RUNNING,
    "转入收尾": STATUS_WRAPUP,
    "关闭事件": STATUS_CLOSED,
}

# 路段管控状态：只有封控段允许从指挥板直接调用材料与班组。
SEGMENT_CLOSED = "封控"
SEGMENT_LIMITED = "限速"
SEGMENT_OPEN = "通行"
SEGMENT_STATUSES = [SEGMENT_CLOSED, SEGMENT_LIMITED, SEGMENT_OPEN]

# 防汛记录中仍在占用道路的状态（已处置、已结束视为不再冲突）。
FLOOD_ACTIVE_STATUSES = {"待响应", "响应中"}


class BusinessError(Exception):
    """业务校验失败：路由层把它翻译成 ok=False 的统一动作结果。"""


def _parse_dt(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise BusinessError(f"缺少必填时间：{field}")
    try:
        # 前端 <input type="datetime-local"> 给的是 "2026-10-02T20:00"。
        return datetime.fromisoformat(text.replace("/", "-"))
    except ValueError as exc:
        raise BusinessError(f"{field} 时间格式无法识别：{text}") from exc


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _require_event(event_id: int) -> dict[str, Any]:
    event = store.find(MODULE, event_id)
    if event is None:
        raise BusinessError(f"风雪事件 {event_id} 不存在或已归档")
    return event


class StormService:
    """指挥板全部业务规则的单入口，带一把进程内互斥锁保证事务串行。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seeded = False

    # ------------------------------------------------------------------ 数据准备
    def ensure_seed(self) -> None:
        """惰性补齐指挥板示例数据：只补一次，且不动其它模块已有记录。"""
        if self._seeded:
            return
        with self._lock:
            if self._seeded:
                return
            self._seed()
            self._seeded = True

    def _seed(self) -> None:
        # 给通用材料台账补库存数量，指挥板扣减库存才有意义；原表没有数量列。
        for row in store.rows("material"):
            row.setdefault("库存数量", 20)
            row.setdefault("计量单位", "吨")
        # 预置执行中事件占用了 1、2 号车，留 3 号车在库待命，方便调度员继续派发结论。
        spare = store.find("vehicle", 3)
        if spare is not None and spare.get("status") == "维修":
            spare["status"] = "在库"

        if store.rows(MODULE):
            return

        def add(table: str, row: dict[str, Any]) -> dict[str, Any]:
            item = {"id": _next_id(table)}
            item.update(row)
            store.rows(table).append(item)
            return item

        # 事件一：执行中的寒潮降雪（预置两个封控段、两次派发结论）。
        e1 = add(MODULE, {
            "事件编号": "STORM-2026-1001",
            "事件名称": "国庆首轮寒潮降雪",
            "天气基准": "暴雪橙色预警｜中到大雪 转 小雪｜气温 -8℃～-2℃｜西北风 5 级",
            "气象开始": "2026-09-30T20:00",
            "气象结束": "2026-10-01T12:00",
            "应急指挥令": "",
            "status": STATUS_RUNNING,
            "pending": True,
            "abnormal": False,
            "备注": "重点保障高架与匝道",
            "created_at": "2026-09-30T17:40",
            "closed_at": "",
        })
        phases1 = [
            {"name": "雪前预撒", "start": "2026-09-30T20:00", "end": "2026-09-30T23:00", "weather": "小雪｜-3℃｜西北4级"},
            {"name": "雪中除雪", "start": "2026-09-30T23:00", "end": "2026-10-01T08:00", "weather": "中到大雪｜-6℃｜西北5级"},
            {"name": "雪后收尾", "start": "2026-10-01T08:00", "end": "2026-10-01T12:00", "weather": "小雪转多云｜-2℃｜西北3级"},
        ]
        segments1 = [
            {"road": "北二环高架 K3+200~K6+800", "segment_status": SEGMENT_CLOSED},
            # 该封控段同时有在办防汛，演示除雪防汛同段占用须凭指挥令定优先级。
            {"road": "迎宾大道 K0+000~K4+500", "segment_status": SEGMENT_CLOSED},
            {"road": "南山路匝道 K0+000~K2+100", "segment_status": SEGMENT_LIMITED},
        ]
        self._materialize_windows(e1, segments1, phases1)

        # 叠加一条在办防汛：与迎宾大道封控段同时占用道路（未关闭的冲突）。
        store.rows("flood").append({
            "id": _next_id("flood"),
            "status": "响应中",
            "pending": True,
            "abnormal": True,
            "记录编号": "FLOO-2026-1001-07",
            "预警级别": "暴雨橙色预警",
            "影响路段": "迎宾大道 K0+000~K4+500",
            "积水深度": "局部 15cm",
            "应急措施": "强排 + 除雪防滑同段抢通，待指挥令定优先级",
            "投入人员": "防汛三班 12 人",
            "恢复时间": "",
            "防汛状态": "响应中",
        })

        # 事件二：待启动的大风吹雪过程。
        e2 = add(MODULE, {
            "事件编号": "STORM-2026-1003",
            "事件名称": "大风吹雪过境",
            "天气基准": "大风蓝色预警｜阵雪｜气温 -12℃～-4℃｜阵风 7 级",
            "气象开始": "2026-10-03T06:00",
            "气象结束": "2026-10-03T18:00",
            "应急指挥令": "",
            "status": STATUS_PENDING,
            "pending": True,
            "abnormal": False,
            "备注": "关注风口路段风吹雪",
            "created_at": "2026-10-01T09:00",
            "closed_at": "",
        })
        phases2 = [
            {"name": "雪中除雪", "start": "2026-10-03T06:00", "end": "2026-10-03T12:00", "weather": "阵雪｜-8℃｜阵风7级"},
            {"name": "雪后收尾", "start": "2026-10-03T12:00", "end": "2026-10-03T18:00", "weather": "多云｜-4℃｜西北风4级"},
        ]
        segments2 = [{"road": "机场高速联络线 K1+000~K9+000", "segment_status": SEGMENT_CLOSED}]
        self._materialize_windows(e2, segments2, phases2)

        # 事件三：已关闭的历史过程，天气基准与台账按当时口径冻结。
        e3 = add(MODULE, {
            "事件编号": "STORM-2026-0908",
            "事件名称": "九月初异常降雪",
            "天气基准": "道路结冰黄色预警｜小雨夹雪｜气温 -1℃～3℃｜东北风 3 级（历史快照，不再更新）",
            "气象开始": "2026-09-08T18:00",
            "气象结束": "2026-09-09T06:00",
            "应急指挥令": "YJ-2026-0908-02",
            "status": STATUS_CLOSED,
            "pending": False,
            "abnormal": False,
            "备注": "与秋季汛情叠加，按指挥令除雪优先",
            "created_at": "2026-09-08T16:00",
            "closed_at": "2026-09-09T07:30",
        })
        phases3 = [
            {"name": "雪中除雪", "start": "2026-09-08T18:00", "end": "2026-09-09T02:00", "weather": "小雨夹雪｜0℃｜东北3级"},
            {"name": "雪后收尾", "start": "2026-09-09T02:00", "end": "2026-09-09T06:00", "weather": "阴｜2℃｜东北2级"},
        ]
        segments3 = [{"road": "河滨东路 K2+000~K5+600", "segment_status": SEGMENT_CLOSED}]
        self._materialize_windows(e3, segments3, phases3)

        self._seed_dispatch(
            e1, window_id=self._window_id(e1["id"], 0, 0),
            op_no="SEED-OP-1001-01", crew="甲班除雪一组",
            vehicles=[1], materials=[(1, 3.0)],
            road=segments1[0]["road"], command_no="",
            task="预撒环保融雪剂，封闭主车道",
            created_at="2026-09-30T20:10",
        )
        self._seed_dispatch(
            e1, window_id=self._window_id(e1["id"], 0, 1),
            op_no="SEED-OP-1001-02", crew="乙班除雪二组",
            vehicles=[2], materials=[(1, 5.0), (2, 12.0)],
            road=segments1[0]["road"], command_no="",
            task="雪中编队除雪，循环撒布",
            created_at="2026-09-30T23:20",
        )
        self._seed_dispatch(
            e3, window_id=self._window_id(e3["id"], 0, 0),
            op_no="SEED-OP-0908-01", crew="甲班除雪一组",
            vehicles=[1], materials=[(1, 4.0)],
            road=segments3[0]["road"], command_no="YJ-2026-0908-02",
            task="汛情叠加段按指挥令优先除雪",
            created_at="2026-09-08T18:30",
        )

    def _materialize_windows(
        self,
        event: dict[str, Any],
        segments: list[dict[str, str]],
        phases: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """把 路段 × 气象时段 切成连续窗口并落表（编排校验通过后才允许调用）。"""
        windows: list[dict[str, Any]] = []
        for seg_idx, seg in enumerate(segments):
            for phase_idx, phase in enumerate(phases):
                item = {
                    "id": _next_id(WINDOW_MODULE),
                    "事件ID": event["id"],
                    "窗口编号": f"{event['事件编号']}-W{seg_idx + 1}-{phase_idx + 1}",
                    "作业路段": seg["road"],
                    "路段状态": seg["segment_status"],
                    "时段名称": phase["name"],
                    "开始时间": phase["start"],
                    "结束时间": phase["end"],
                    "时段天气": phase["weather"],
                    "班组": "",
                    "作业车辆": "",
                    "窗口状态": "待作业" if event["status"] != STATUS_CLOSED else "已完结",
                }
                store.rows(WINDOW_MODULE).append(item)
                windows.append(item)
        return windows

    def _window_id(self, event_id: int, seg_idx: int, phase_idx: int) -> int:
        for row in store.rows(WINDOW_MODULE):
            if int(row["事件ID"]) != event_id:
                continue
            parts = str(row["窗口编号"]).split("-W")[-1].split("-")
            if [int(p) for p in parts] == [seg_idx + 1, phase_idx + 1]:
                return int(row["id"])
        raise BusinessError("预置窗口定位失败")

    def _seed_dispatch(
        self,
        event: dict[str, Any],
        *,
        window_id: int,
        op_no: str,
        crew: str,
        vehicles: list[int],
        materials: list[tuple[int, float]],
        road: str,
        command_no: str,
        task: str,
        created_at: str,
    ) -> None:
        """示例派发：绕过运行期校验，直接构造事务产物，库存按示例口径预扣。"""
        window = store.find(WINDOW_MODULE, window_id)
        dispatch_id = _next_id(DISPATCH_MODULE)
        dispatch_no = f"DSP-{event['事件编号']}-{dispatch_id:03d}"
        store.rows(DISPATCH_MODULE).append({
            "id": dispatch_id,
            "派发编号": dispatch_no,
            "操作号": op_no,
            "事件ID": event["id"],
            "窗口ID": window_id,
            "作业路段": road,
            "班组": crew,
            "车辆IDs": ",".join(str(v) for v in vehicles),
            "材料": "；".join(f"{mid}:{qty}" for mid, qty in materials),
            "应急指挥令": command_no,
            "派发说明": task,
            "天气基准": event["天气基准"],
            "时段天气": window["时段天气"],
            "status": "已派发",
            "pending": event["status"] != STATUS_CLOSED,
            "created_at": created_at,
        })
        store.rows(OPLOG_MODULE).append({
            "id": _next_id(OPLOG_MODULE),
            "操作号": op_no,
            "操作类型": "dispatch",
            "事件ID": event["id"],
            "结果": {"ok": True, "dispatch_id": dispatch_id, "seed": True},
            "created_at": created_at,
        })
        window["班组"] = crew
        window["作业车辆"] = ",".join(str(v) for v in vehicles)
        window["窗口状态"] = "作业中" if event["status"] == STATUS_RUNNING else "已完结"

        store.rows("winter").append({
            "id": _next_id("winter"),
            "status": "作业中" if event["status"] == STATUS_RUNNING else "已完成",
            "pending": event["status"] != STATUS_CLOSED,
            "abnormal": False,
            "事件ID": event["id"],
            "派发编号": dispatch_no,
            "作业编号": f"WINT-EVENT-{dispatch_id:04d}",
            "作业路段": road,
            "作业日期": created_at[:10],
            "融雪剂用量": "；".join(f"材料{mid} {qty}吨" for mid, qty in materials),
            "作业车辆": ",".join(str(v) for v in vehicles),
            "作业班组": crew,
            "路面状况": window["时段天气"],
            "作业状态": "作业中" if event["status"] == STATUS_RUNNING else "已完成",
            "天气基准": event["天气基准"],
        })
        store.rows(ROAD_PHASE_MODULE).append({
            "id": _next_id(ROAD_PHASE_MODULE),
            "事件ID": event["id"],
            "派发编号": dispatch_no,
            "作业路段": road,
            "阶段": window["时段名称"],
            "开始时间": window["开始时间"],
            "结束时间": window["结束时间"],
            "管控状态": window["路段状态"],
            "班组": crew,
            "天气基准": event["天气基准"],
            "status": "进行中" if event["status"] == STATUS_RUNNING else "已关闭",
            "created_at": created_at,
        })
        for mid, qty in materials:
            material = store.find("material", mid)
            store.rows(MATERIAL_ITEM_MODULE).append({
                "id": _next_id(MATERIAL_ITEM_MODULE),
                "事件ID": event["id"],
                "派发编号": dispatch_no,
                "材料ID": mid,
                "材料名称": material["材料名称"] if material else f"材料{mid}",
                "扣减数量": qty,
                "计量单位": material.get("计量单位", "吨") if material else "吨",
                "作业路段": road,
                "用途说明": task,
                "天气基准": event["天气基准"],
                "status": "已扣减",
                "created_at": created_at,
            })
            if material is not None:
                material["库存数量"] = max(0.0, float(material.get("库存数量", 0)) - qty)
        for vid in vehicles:
            store.rows(VEHICLE_OCC_MODULE).append({
                "id": _next_id(VEHICLE_OCC_MODULE),
                "事件ID": event["id"],
                "派发编号": dispatch_no,
                "车辆ID": vid,
                "作业路段": road,
                "占用开始": created_at,
                "占用结束": "",
                "status": "占用中" if event["status"] == STATUS_RUNNING else "已释放",
            })
            vehicle = store.find("vehicle", vid)
            if vehicle is not None and event["status"] == STATUS_RUNNING:
                vehicle["status"] = "出车作业"

    # ------------------------------------------------------------------ 指挥板
    def board(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
    ) -> dict[str, Any]:
        """聚合指挥板：事件列表 + 连续窗口 + 车辆实时位置 + 资源余量。"""
        self.ensure_seed()
        events = list(store.rows(MODULE))
        if status:
            events = [e for e in events if e["status"] == status]
        if keyword:
            events = [
                e for e in events
                if keyword in str(e["事件编号"]) or keyword in str(e["事件名称"])
            ]
        event_ids = {int(e["id"]) for e in events}
        windows = [
            w for w in store.rows(WINDOW_MODULE)
            if int(w["事件ID"]) in event_ids
        ]
        windows.sort(key=lambda w: (int(w["事件ID"]), w["开始时间"], w["作业路段"]))
        positions = sorted(
            store.rows(POSITION_MODULE),
            key=lambda p: (int(p["事件ID"]), str(p["上报时间"])),
        )
        materials = [
            {
                "id": m["id"],
                "材料编号": m.get("材料编号"),
                "材料名称": m.get("材料名称"),
                "库存数量": m.get("库存数量", 0),
                "计量单位": m.get("计量单位", "吨"),
                "status": m.get("status"),
            }
            for m in store.rows("material")
        ]
        vehicles = [
            {
                "id": v["id"],
                "车辆编号": v.get("车辆编号"),
                "车牌号": v.get("车牌号"),
                "车辆类型": v.get("车辆类型"),
                "status": v.get("status"),
            }
            for v in store.rows("vehicle")
        ]
        flood_active = [
            {"记录编号": f.get("记录编号"), "影响路段": f.get("影响路段"), "status": f.get("status")}
            for f in store.rows("flood")
            if f.get("status") in FLOOD_ACTIVE_STATUSES
        ]
        cards = [
            {"label": "待启动", "value": sum(1 for e in store.rows(MODULE) if e["status"] == STATUS_PENDING)},
            {"label": "执行中", "value": sum(1 for e in store.rows(MODULE) if e["status"] == STATUS_RUNNING)},
            {"label": "收尾", "value": sum(1 for e in store.rows(MODULE) if e["status"] == STATUS_WRAPUP)},
            {"label": "已关闭", "value": sum(1 for e in store.rows(MODULE) if e["status"] == STATUS_CLOSED)},
            {"label": "在途车辆", "value": sum(1 for v in vehicles if v["status"] == "出车作业")},
        ]
        return {
            "events": sorted(events, key=lambda e: str(e["气象开始"]), reverse=True),
            "windows": windows,
            "positions": positions,
            "materials": materials,
            "vehicles": vehicles,
            "flood_active": flood_active,
            "cards": cards,
        }

    def event_detail(self, event_id: int) -> dict[str, Any]:
        """事件详情：三处台账（除雪台账/路段阶段/物资清单）必须引用同一事件。"""
        self.ensure_seed()
        event = _require_event(event_id)
        windows = sorted(
            (w for w in store.rows(WINDOW_MODULE) if int(w["事件ID"]) == event_id),
            key=lambda w: (w["作业路段"], w["开始时间"]),
        )
        dispatches = [
            d for d in store.rows(DISPATCH_MODULE) if int(d["事件ID"]) == event_id
        ]
        return {
            "event": event,
            "windows": windows,
            "dispatches": sorted(dispatches, key=lambda d: str(d["created_at"])),
            "winter_ledger": [
                r for r in store.rows("winter") if int(r.get("事件ID", 0)) == event_id
            ],
            "road_phases": [
                r for r in store.rows(ROAD_PHASE_MODULE) if int(r.get("事件ID", 0)) == event_id
            ],
            "material_items": [
                r for r in store.rows(MATERIAL_ITEM_MODULE) if int(r.get("事件ID", 0)) == event_id
            ],
            "vehicle_occ": [
                r for r in store.rows(VEHICLE_OCC_MODULE) if int(r.get("事件ID", 0)) == event_id
            ],
            "positions": [
                r for r in store.rows(POSITION_MODULE) if int(r.get("事件ID", 0)) == event_id
            ],
        }

    # ------------------------------------------------------------------ 建事件 / 编排
    def create_event(self, values: dict[str, Any], *, op_no: str = "") -> dict[str, Any]:
        """登记风雪事件并立即把气象时段与作业路段编排成连续窗口。"""
        self.ensure_seed()
        with self._lock:
            existed = self._replayed(op_no)
            if existed is not None:
                return existed["结果"]
            required = ["事件名称", "天气基准", "气象开始", "气象结束"]
            missing = [f for f in required if not str(values.get(f) or "").strip()]
            if missing:
                raise BusinessError(f"缺少必填字段：{'、'.join(missing)}")
            start = _parse_dt(values.get("气象开始"), "气象开始")
            end = _parse_dt(values.get("气象结束"), "气象结束")
            if end <= start:
                raise BusinessError("气象结束时间必须晚于气象开始时间")

            phases = self._normalize_phases(values.get("气象时段") or [], start, end)
            segments = self._normalize_segments(values.get("作业路段") or [])
            if not segments:
                raise BusinessError("至少要编排一个作业路段")

            rows = store.rows(MODULE)
            event_id = _next_id(MODULE)
            event = {
                "id": event_id,
                "事件编号": str(values.get("事件编号") or f"STORM-2026-{event_id:04d}"),
                "事件名称": str(values["事件名称"]).strip(),
                "天气基准": str(values["天气基准"]).strip(),
                "气象开始": values["气象开始"],
                "气象结束": values["气象结束"],
                "应急指挥令": str(values.get("应急指挥令") or "").strip(),
                "status": STATUS_PENDING,
                "pending": True,
                "abnormal": False,
                "备注": str(values.get("备注") or "").strip(),
                "created_at": datetime.now().isoformat(timespec="minutes"),
                "closed_at": "",
            }
            rows.append(event)
            windows = self._materialize_windows(
                event,
                [{"road": s["road"], "segment_status": s["segment_status"]} for s in segments],
                [{"name": p["name"], "start": p["start"], "end": p["end"], "weather": p["weather"]} for p in phases],
            )
            result = {
                "ok": True,
                "message": f"风雪事件已登记，编排 {len(windows)} 个连续事件窗口",
                "event": event,
                "windows": windows,
            }
            self._record_op(op_no, "create_event", event_id, result)
            return result

    def _normalize_phases(
        self, raw: list[dict[str, Any]], event_start: datetime, event_end: datetime
    ) -> list[dict[str, str]]:
        """校验气象时段首尾相接、恰好铺满整个气象过程，不允许间隙或重叠。"""
        if not raw:
            raise BusinessError("至少要编排一个气象时段")
        phases: list[dict[str, str]] = []
        for index, item in enumerate(raw):
            name = str(item.get("name") or item.get("时段名称") or f"气象时段{index + 1}").strip()
            weather = str(item.get("weather") or item.get("时段天气") or "").strip()
            start = _parse_dt(item.get("start") or item.get("开始时间"), f"{name}开始")
            end = _parse_dt(item.get("end") or item.get("结束时间"), f"{name}结束")
            if end <= start:
                raise BusinessError(f"气象时段「{name}」结束时间必须晚于开始时间")
            phases.append({
                "name": name,
                "weather": weather,
                "start": item.get("start") or item.get("开始时间"),
                "end": item.get("end") or item.get("结束时间"),
                "_start_dt": start,
                "_end_dt": end,
            })
        phases.sort(key=lambda p: p["_start_dt"])
        if phases[0]["_start_dt"] != event_start:
            raise BusinessError("首个气象时段必须从气象开始时间起算，窗口不允许留有间隙")
        if phases[-1]["_end_dt"] != event_end:
            raise BusinessError("末个气象时段必须到气象结束时间收口，窗口不允许留有间隙")
        for prev, curr in zip(phases, phases[1:]):
            if prev["_end_dt"] != curr["_start_dt"]:
                relation = "重叠" if prev["_end_dt"] > curr["_start_dt"] else "存在间隙"
                raise BusinessError(
                    f"气象时段不连续：「{prev['name']}」与「{curr['name']}」{relation}，"
                    "事件窗口必须首尾相接"
                )
        return phases

    def _normalize_segments(self, raw: list[dict[str, Any]]) -> list[dict[str, str]]:
        segments: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            road = str(item.get("road") or item.get("作业路段") or "").strip()
            if not road:
                continue
            if road in seen:
                raise BusinessError(f"作业路段「{road}」重复编排，同一事件内不允许重复占段")
            seen.add(road)
            segment_status = str(item.get("segment_status") or item.get("路段状态") or SEGMENT_CLOSED).strip()
            if segment_status not in SEGMENT_STATUSES:
                raise BusinessError(f"路段「{road}」管控状态不合法：{segment_status}")
            segments.append({"road": road, "segment_status": segment_status})
        return segments

    # ------------------------------------------------------------------ 状态流转
    def transition(self, event_id: int, action: str, *, op_no: str = "") -> dict[str, Any]:
        """事件状态机：只能 待启动→执行中→收尾→已关闭 顺序推进一格。"""
        self.ensure_seed()
        with self._lock:
            existed = self._replayed(op_no)
            if existed is not None:
                return existed["结果"]
            event = _require_event(event_id)
            current = event["status"]
            if current == STATUS_CLOSED:
                raise BusinessError("事件已关闭，状态不能回退或重开；历史作业按当时天气基准保留")
            if action not in TRANSITIONS:
                raise BusinessError(f"动作「{action}」不属于风雪事件可执行范围")
            expected_action, target = NEXT_ACTION[current]
            if action != expected_action:
                raise BusinessError(
                    f"事件当前为「{current}」，只能执行「{expected_action}」，"
                    f"不能跳到「{TRANSITIONS[action]}」"
                )
            event["status"] = target
            event["pending"] = target != STATUS_CLOSED
            if target == STATUS_CLOSED:
                event["closed_at"] = datetime.now().isoformat(timespec="minutes")
                self._wrapup_event(event)
            result = {"ok": True, "message": f"风雪事件已{action}：{current} → {target}", "event": event}
            self._record_op(op_no, "transition", event_id, result)
            return result

    def _wrapup_event(self, event: dict[str, Any]) -> None:
        """关闭时统一收尾：车辆释放归库、窗口与路段阶段收口，台账与天气基准冻结。"""
        event_id = int(event["id"])
        now = event["closed_at"]
        for occ in store.rows(VEHICLE_OCC_MODULE):
            if int(occ["事件ID"]) == event_id and occ["status"] == "占用中":
                occ["status"] = "已释放"
                occ["占用结束"] = now
                vehicle = store.find("vehicle", int(occ["车辆ID"]))
                if vehicle is not None and vehicle["status"] == "出车作业":
                    vehicle["status"] = "在库"
        for window in store.rows(WINDOW_MODULE):
            if int(window["事件ID"]) == event_id and window["窗口状态"] != "已完结":
                window["窗口状态"] = "已完结"
        for phase in store.rows(ROAD_PHASE_MODULE):
            if int(phase.get("事件ID", 0)) == event_id and phase["status"] != "已关闭":
                phase["status"] = "已关闭"
        for row in store.rows("winter"):
            if int(row.get("事件ID", 0)) == event_id:
                row["status"] = "已完成"
                row["pending"] = False
                row["作业状态"] = "已完成"

    # ------------------------------------------------------------------ 调度事务
    def dispatch(self, values: dict[str, Any], *, op_no: str = "") -> dict[str, Any]:
        """调度会话下结论：派发任务、占用车辆、扣减材料在同一事务内提交。"""
        self.ensure_seed()
        with self._lock:
            existed = self._replayed(op_no)
            if existed is not None:
                return existed["结果"]

            event_id = int(values.get("事件ID") or 0)
            event = _require_event(event_id)
            if event["status"] == STATUS_CLOSED:
                raise BusinessError("事件已关闭，历史台账冻结，不允许再派发任务")
            if event["status"] != STATUS_RUNNING:
                raise BusinessError(f"事件当前为「{event['status']}」，只有执行中的事件才能派发调度结论")

            window_id = int(values.get("窗口ID") or 0)
            window = store.find(WINDOW_MODULE, window_id)
            if window is None or int(window["事件ID"]) != event_id:
                raise BusinessError("调度窗口不属于该事件，请重新选择连续事件窗口")

            crew = str(values.get("班组") or "").strip()
            task = str(values.get("派发说明") or "").strip()
            if not crew:
                raise BusinessError("缺少调用人班组，封控段处置必须明确责任班组")
            if not task:
                raise BusinessError("缺少调度会话结论（派发说明）")

            # 封控段才能直接调用材料与班组；其余管控状态走常规流程。
            if window["路段状态"] != SEGMENT_CLOSED:
                raise BusinessError(
                    f"路段当前为「{window['路段状态']}」，只有封控段可从指挥板直接调用材料与班组"
                )

            # 除雪与防汛同时占用道路：必须凭应急指挥令定优先级。
            command_no = str(values.get("应急指挥令") or "").strip()
            conflict = self._flood_conflict(window["作业路段"])
            if conflict is not None and not command_no:
                raise BusinessError(
                    f"路段「{window['作业路段']}」正被防汛记录 {conflict['记录编号']}（{conflict['status']}）占用，"
                    "除雪与防汛同时占用道路须凭应急指挥令确定优先级后再派发"
                )

            vehicle_ids = [int(v) for v in (values.get("车辆IDs") or []) if str(v).strip()]
            if not vehicle_ids:
                raise BusinessError("至少调度一台作业车辆")
            if len(vehicle_ids) != len(set(vehicle_ids)):
                raise BusinessError("同一台车在一次派发中重复出现，请核对车辆占用")
            for vid in vehicle_ids:
                vehicle = store.find("vehicle", vid)
                if vehicle is None:
                    raise BusinessError(f"车辆 {vid} 不存在")
                if self._vehicle_busy(vid, event_id):
                    raise BusinessError(
                        f"车辆 {vehicle.get('车牌号') or vid} 已被其它在办事件占用，任务派发与车辆占用不一致"
                    )
                if vehicle.get("status") not in ("在库", None, ""):
                    raise BusinessError(
                        f"车辆 {vehicle.get('车牌号') or vid} 当前状态为「{vehicle.get('status')}」，不可派车"
                    )

            materials_raw = values.get("材料") or []
            material_lines: list[dict[str, Any]] = []
            for line in materials_raw:
                mid = int(line.get("材料ID") or 0)
                qty = float(line.get("数量") or 0)
                if mid <= 0 or qty <= 0:
                    continue
                material = store.find("material", mid)
                if material is None:
                    raise BusinessError(f"材料 {mid} 不存在")
                if float(material.get("库存数量", 0)) + 1e-9 < qty:
                    raise BusinessError(
                        f"材料「{material['材料名称']}」库存 {material.get('库存数量', 0)}"
                        f"{material.get('计量单位', '吨')}，不足扣减 {qty}{material.get('计量单位', '吨')}"
                    )
                material_lines.append({"material": material, "qty": qty})
            if not material_lines:
                raise BusinessError("封控段调用材料至少填写一种材料的扣减数量")

            # ---- 全部校验通过，以下为原子提交；内存模型无回滚日志，故不允许中途抛错 ----
            now = datetime.now().isoformat(timespec="minutes")
            dispatch_id = _next_id(DISPATCH_MODULE)
            dispatch_no = f"DSP-{event['事件编号']}-{dispatch_id:03d}"
            dispatch = {
                "id": dispatch_id,
                "派发编号": dispatch_no,
                "操作号": op_no,
                "事件ID": event_id,
                "窗口ID": window_id,
                "作业路段": window["作业路段"],
                "班组": crew,
                "车辆IDs": ",".join(str(v) for v in vehicle_ids),
                "材料": "；".join(f"{line['material']['id']}:{line['qty']}" for line in material_lines),
                "应急指挥令": command_no,
                "派发说明": task,
                "天气基准": event["天气基准"],
                "时段天气": window["时段天气"],
                "status": "已派发",
                "pending": True,
                "created_at": now,
            }
            store.rows(DISPATCH_MODULE).append(dispatch)

            # ① 除雪作业台账
            ledger = {
                "id": _next_id("winter"),
                "status": "作业中",
                "pending": True,
                "abnormal": False,
                "事件ID": event_id,
                "派发编号": dispatch_no,
                "作业编号": f"WINT-EVENT-{dispatch_id:04d}",
                "作业路段": window["作业路段"],
                "作业日期": now[:10],
                "融雪剂用量": "；".join(
                    f"{line['material']['材料名称']} {line['qty']}{line['material'].get('计量单位', '吨')}"
                    for line in material_lines
                ),
                "作业车辆": ",".join(str(v) for v in vehicle_ids),
                "作业班组": crew,
                "路面状况": window["时段天气"],
                "作业状态": "作业中",
                "天气基准": event["天气基准"],
            }
            store.rows("winter").append(ledger)

            # ② 路段阶段
            road_phase = {
                "id": _next_id(ROAD_PHASE_MODULE),
                "事件ID": event_id,
                "派发编号": dispatch_no,
                "作业路段": window["作业路段"],
                "阶段": window["时段名称"],
                "开始时间": window["开始时间"],
                "结束时间": window["结束时间"],
                "管控状态": window["路段状态"],
                "班组": crew,
                "天气基准": event["天气基准"],
                "status": "进行中",
                "created_at": now,
            }
            store.rows(ROAD_PHASE_MODULE).append(road_phase)

            # ③ 物资清单（扣减）
            for line in material_lines:
                material = line["material"]
                store.rows(MATERIAL_ITEM_MODULE).append({
                    "id": _next_id(MATERIAL_ITEM_MODULE),
                    "事件ID": event_id,
                    "派发编号": dispatch_no,
                    "材料ID": material["id"],
                    "材料名称": material["材料名称"],
                    "扣减数量": line["qty"],
                    "计量单位": material.get("计量单位", "吨"),
                    "作业路段": window["作业路段"],
                    "用途说明": task,
                    "天气基准": event["天气基准"],
                    "status": "已扣减",
                    "created_at": now,
                })
                material["库存数量"] = round(
                    float(material.get("库存数量", 0)) - line["qty"], 3
                )
                if material["status"] == "在库" and float(material["库存数量"]) <= 0:
                    material["status"] = "已领用"

            # 车辆占用 + 窗口回填
            for vid in vehicle_ids:
                store.rows(VEHICLE_OCC_MODULE).append({
                    "id": _next_id(VEHICLE_OCC_MODULE),
                    "事件ID": event_id,
                    "派发编号": dispatch_no,
                    "车辆ID": vid,
                    "作业路段": window["作业路段"],
                    "占用开始": now,
                    "占用结束": "",
                    "status": "占用中",
                })
                vehicle = store.find("vehicle", vid)
                if vehicle is not None:
                    vehicle["status"] = "出车作业"
            window["班组"] = crew
            window["作业车辆"] = ",".join(str(v) for v in vehicle_ids)
            window["窗口状态"] = "作业中"

            result = {
                "ok": True,
                "message": f"调度结论已写入除雪台账、路段阶段与物资清单（同引事件 {event['事件编号']}）",
                "dispatch": dispatch,
                "event_id": event_id,
                "ledger": ledger,
                "road_phase": road_phase,
                "material_items": [
                    r for r in store.rows(MATERIAL_ITEM_MODULE)
                    if r.get("派发编号") == dispatch_no
                ],
                "conflict_note": (
                    f"与防汛 {conflict['记录编号']} 同段，按指挥令 {command_no} 确定除雪优先"
                    if conflict is not None else ""
                ),
            }
            self._record_op(op_no, "dispatch", event_id, result)
            return result

    def _flood_conflict(self, road: str) -> dict[str, Any] | None:
        """同一路段在防汛台账中仍处于待响应/响应中，即视为道路同时被占用。"""
        for flood in store.rows("flood"):
            if flood.get("status") not in FLOOD_ACTIVE_STATUSES:
                continue
            if road == str(flood.get("影响路段") or "").strip():
                return flood
        return None

    def _vehicle_busy(self, vehicle_id: int, event_id: int) -> bool:
        for occ in store.rows(VEHICLE_OCC_MODULE):
            if int(occ["车辆ID"]) == vehicle_id and occ["status"] == "占用中":
                return True
        return False

    # ------------------------------------------------------------------ 车辆位置
    def report_position(self, values: dict[str, Any], *, op_no: str = "") -> dict[str, Any]:
        """把车辆实时位置挂进事件窗口，是窗口时间线上的位置切片。"""
        self.ensure_seed()
        with self._lock:
            existed = self._replayed(op_no)
            if existed is not None:
                return existed["结果"]
            event_id = int(values.get("事件ID") or 0)
            event = _require_event(event_id)
            if event["status"] == STATUS_CLOSED:
                raise BusinessError("事件已关闭，车辆位置随历史天气基准一并冻结，不再接收上报")
            vehicle_id = int(values.get("车辆ID") or 0)
            vehicle = store.find("vehicle", vehicle_id)
            if vehicle is None:
                raise BusinessError(f"车辆 {vehicle_id} 不存在")
            location = str(values.get("位置") or values.get("桩号") or "").strip()
            if not location:
                raise BusinessError("缺少车辆位置（桩号）")
            window_id = int(values.get("窗口ID") or 0)
            if window_id:
                window = store.find(WINDOW_MODULE, window_id)
                if window is None or int(window["事件ID"]) != event_id:
                    raise BusinessError("位置上报绑定的窗口不属于该事件")
            reported = str(values.get("上报时间") or datetime.now().isoformat(timespec="minutes"))
            position = {
                "id": _next_id(POSITION_MODULE),
                "操作号": op_no,
                "事件ID": event_id,
                "窗口ID": window_id,
                "车辆ID": vehicle_id,
                "车牌号": vehicle.get("车牌号"),
                "位置": location,
                "上报时间": reported,
            }
            store.rows(POSITION_MODULE).append(position)
            result = {"ok": True, "message": f"车辆 {vehicle.get('车牌号') or vehicle_id} 位置已挂接事件窗口", "position": position}
            self._record_op(op_no, "report_position", event_id, result)
            return result

    # ------------------------------------------------------------------ 操作日志 / 幂等
    def list_ops(self, event_id: int | None = None) -> list[dict[str, Any]]:
        self.ensure_seed()
        rows = store.rows(OPLOG_MODULE)
        if event_id is not None:
            rows = [r for r in rows if int(r.get("事件ID", 0)) == event_id]
        return sorted(rows, key=lambda r: int(r["id"]), reverse=True)

    def _replayed(self, op_no: str) -> dict[str, Any] | None:
        """同一操作号重放：直接回放首次结果，绝不第二次扣减库存或占用车辆。"""
        if not op_no:
            return None
        for row in store.rows(OPLOG_MODULE):
            if row.get("操作号") == op_no:
                result = dict(row["结果"])
                result["replayed"] = True
                result["message"] = f"操作号 {op_no} 已处理过，按首次结果幂等返回，未重复扣减"
                return {"结果": result}
        return None

    def _record_op(
        self, op_no: str, op_type: str, event_id: int, result: dict[str, Any]
    ) -> None:
        if not op_no:
            return
        store.rows(OPLOG_MODULE).append({
            "id": _next_id(OPLOG_MODULE),
            "操作号": op_no,
            "操作类型": op_type,
            "事件ID": event_id,
            "结果": result,
            "created_at": datetime.now().isoformat(timespec="minutes"),
        })

    def sync_batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """断网复位后的批量续传：逐条按操作号幂等回放，单条失败不影响其它条目。"""
        self.ensure_seed()
        handlers = {
            "create_event": lambda v, no: self.create_event(v, op_no=no),
            "transition": lambda v, no: self.transition(int(v["事件ID"]), str(v["action"]), op_no=no),
            "dispatch": lambda v, no: self.dispatch(v, op_no=no),
            "report_position": lambda v, no: self.report_position(v, op_no=no),
        }
        results: list[dict[str, Any]] = []
        synced = 0
        replayed = 0
        for op in operations:
            op_no = str(op.get("操作号") or "").strip()
            op_type = str(op.get("操作类型") or "").strip()
            values = op.get("values") or {}
            if not op_no:
                results.append({"操作号": "", "ok": False, "message": "缺少操作号，无法幂等续传"})
                continue
            if op_type not in handlers:
                results.append({"操作号": op_no, "ok": False, "message": f"不支持的操作类型：{op_type}"})
                continue
            try:
                result = handlers[op_type](values, op_no)
                results.append({
                    "操作号": op_no,
                    "ok": True,
                    "replayed": bool(result.get("replayed")),
                    "message": result.get("message", ""),
                })
                if result.get("replayed"):
                    replayed += 1
                else:
                    synced += 1
            except BusinessError as exc:
                results.append({"操作号": op_no, "ok": False, "message": str(exc)})
        return {
            "ok": True,
            "message": f"续传完成：新提交 {synced} 条，幂等跳过 {replayed} 条，共 {len(operations)} 条",
            "synced": synced,
            "replayed": replayed,
            "results": results,
        }


storm_service = StormService()
