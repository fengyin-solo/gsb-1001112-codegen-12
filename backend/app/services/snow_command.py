"""风雪事件指挥板业务规则。

把气象时段、作业路段（封控段）与车辆位置编排为同一个风雪事件下的连续窗口；
调度会话结论同时写入「除雪作业台账 / 路段阶段 / 物资清单」三处，三处都带同一事件引用。

约束集中在本模块：
- 事件状态严格按 待启动 → 执行中 → 收尾 → 已关闭 流转，不能跳步，已关闭为终态；
  路段阶段用同一序列、同一规则。
- 任务派发、车辆占用、材料扣减在同一事务内完成，任一校验不过全部不落地。
- 除雪与防汛同时占用道路时，以现行有效的应急指挥令确定优先方；无令则暂停除雪派发。
- 历史作业按事件当时冻结的天气基准保留，台账行落表时即复制快照。
- 所有写操作凭「操作号」幂等：断网期间前端排队，复位后整单续传，重复操作号直接回放结果。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any, Callable

from app.store import store

# ---- 表名 ----------------------------------------------------------------
T_EVENT = "snow_event"
T_SEGMENT = "snow_segment"
T_LEDGER = "snow_ledger"
T_MATERIAL = "snow_material"
T_TASK = "snow_dispatch"
T_LOCK = "snow_lock"
T_ORDER = "emergency_order"
T_OPS = "snow_ops"

# ---- 状态机 --------------------------------------------------------------
EVENT_STATUSES = ["待启动", "执行中", "收尾", "已关闭"]
EVENT_ACTIONS = {"启动事件": 1, "转入收尾": 2, "关闭事件": 3}
PHASE_ACTIONS = {"启动路段": 1, "路段收尾": 2, "关闭路段": 3}
TASK_STATUSES = ["已派发", "执行中", "已完成"]
FLOOD_ACTIVE = {"待响应", "响应中"}
TERMINAL = "已关闭"

_big_lock = threading.RLock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _next_no(prefix: str, field: str, table: str) -> str:
    rows = store.rows(table)
    seq = max((int(str(row.get(field, "0")).split("-")[-1]) for row in rows), default=0) + 1
    return f"{prefix}-{seq:04d}"


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message}


class SnowCommandService:
    """风雪事件指挥板：事件窗口、封控段调度与三处台账一致性都在这里收口。"""

    # =====================================================================
    # 幂等与事务
    # =====================================================================
    def _idempotent(
        self,
        op_no: str | None,
        path: str,
        payload: dict[str, Any],
        apply: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """按操作号保证一次写操作只生效一次；复位续传时回放首次结果。"""
        op_no = (op_no or "").strip() or f"OP-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        with _big_lock:
            for row in store.rows(T_OPS):
                if row.get("操作号") == op_no:
                    cached = copy.deepcopy(row["结果"])
                    cached["操作号"] = op_no
                    cached["幂等回放"] = True
                    return cached
            result = apply()
            store.rows(T_OPS).append({
                "id": _next_id(T_OPS),
                "操作号": op_no,
                "路径": path,
                "请求": copy.deepcopy(payload),
                "结果": copy.deepcopy(result),
                "时间": _now(),
            })
            result["操作号"] = op_no
            return result

    @staticmethod
    def _transactional(snapshots: dict[str, list[dict[str, Any]]],
                       apply: Callable[[], Any]) -> Any:
        """先复制相关表，apply 抛错时整表回滚，保证派发/占用/扣减同生共死。"""
        try:
            return apply()
        except Exception:
            for table, backup in snapshots.items():
                store._tables[table] = backup  # noqa: SLF001 - 内存仓库的事务回滚
            raise

    # =====================================================================
    # 查询：事件列表 / 指挥板 / 三处引用核对
    # =====================================================================
    def list_events(self, *, status: str | None = None, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_EVENT)
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("事件编号", "")) or keyword in str(row.get("事件名称", ""))
            ]
        return sorted(rows, key=lambda row: int(row["id"]), reverse=True)

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return store.find(T_EVENT, event_id)

    def _segments(self, event_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(T_SEGMENT) if row.get("事件id") == event_id]

    def _tasks(self, event_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(T_TASK) if row.get("事件id") == event_id]

    def _locks(self, event_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(T_LOCK) if row.get("事件id") == event_id]

    def _active_order(self, road: str) -> dict[str, Any] | None:
        """找当前对该路段生效的指挥令；同一路段多张令时取最新一张。"""
        candidates = [
            row for row in store.rows(T_ORDER)
            if row.get("状态") == "生效" and road in str(row.get("影响路段", ""))
        ]
        candidates.sort(key=lambda row: int(row["id"]), reverse=True)
        return candidates[0] if candidates else None

    def _flood_conflicts(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        road = str(segment.get("路段名称", ""))
        code = str(segment.get("路段编号", ""))
        conflicts = []
        for row in store.rows("flood"):
            if row.get("status") not in FLOOD_ACTIVE:
                continue
            scope = str(row.get("影响路段", ""))
            if road and road in scope or code and code in scope:
                conflicts.append(row)
        return conflicts

    def board(self, event_id: int) -> tuple[dict[str, Any] | None, str]:
        """指挥板：事件头、封控段（含车辆位置）、连续事件窗口、指挥令与防汛冲突。"""
        event = store.find(T_EVENT, event_id)
        if event is None:
            return None, f"风雪事件 {event_id} 不存在"
        segments = self._segments(event_id)
        locks = self._locks(event_id)
        tasks = self._tasks(event_id)

        segment_cards: list[dict[str, Any]] = []
        for segment in segments:
            seg_tasks = [task for task in tasks if task.get("路段id") == segment["id"]]
            seg_locks = [lock for lock in locks if lock.get("路段id") == segment["id"]]
            conflicts = self._flood_conflicts(segment)
            order = self._active_order(str(segment.get("路段名称", "")))
            segment_cards.append({
                **segment,
                "任务": seg_tasks,
                "车辆位置": [lock for lock in seg_locks if lock.get("占用状态") == "占用中"],
                "防汛冲突": [{"记录编号": c.get("记录编号"), "status": c.get("status"),
                          "预警级别": c.get("预警级别")} for c in conflicts],
                "指挥令": ({"令号": order["令号"], "优先方": order["优先方"]} if order else None),
            })

        # 连续事件窗口：气象时段 → 路段阶段 → 车辆位置，按起点排序串联
        windows: list[dict[str, Any]] = [{
            "窗口类型": "气象时段",
            "标题": f"{event['事件编号']} 气象时段（{event.get('天气基准', '')}）",
            "开始": event.get("开始时间"),
            "结束": event.get("结束时间"),
            "状态": event.get("状态"),
        }]
        for segment in segments:
            windows.append({
                "窗口类型": "作业路段",
                "标题": f"{segment.get('路段名称')}（{segment.get('阶段')}）",
                "开始": segment.get("启动时间"),
                "结束": segment.get("关闭时间") or segment.get("收尾时间"),
                "状态": segment.get("阶段"),
            })
        for lock in locks:
            windows.append({
                "窗口类型": "车辆位置",
                "标题": f"{lock.get('车牌号')} 位于 {lock.get('路段名称')}",
                "开始": lock.get("占用起"),
                "结束": lock.get("占用止"),
                "状态": lock.get("占用状态"),
            })
        windows.sort(key=lambda item: str(item.get("开始") or ""))

        return {
            "event": event,
            "segments": segment_cards,
            "tasks": sorted(tasks, key=lambda row: int(row["id"]), reverse=True),
            "ledgers": sorted(
                [row for row in store.rows(T_LEDGER) if row.get("事件id") == event_id],
                key=lambda row: int(row["id"]), reverse=True,
            ),
            "materials": [row for row in store.rows(T_MATERIAL) if row.get("事件id") == event_id],
            "windows": windows,
            "orders": [row for row in store.rows(T_ORDER) if row.get("状态") == "生效"],
        }, ""

    def references(self, event_id: int) -> dict[str, Any]:
        """核对台账、路段阶段、物资清单三处是否全部引用同一事件。"""
        groups = {
            "除雪作业台账": store.rows(T_LEDGER),
            "路段阶段": store.rows(T_SEGMENT),
            "物资清单": store.rows(T_MATERIAL),
        }
        result: dict[str, Any] = {"事件id": event_id, "一致": True, "三处": {}}
        for name, rows in groups.items():
            linked = [row for row in rows if row.get("事件id") == event_id]
            bad = [row for row in linked if row.get("事件id") != event_id]
            result["三处"][name] = {
                "引用数量": len(linked),
                "引用编号": [row.get("台账编号") or row.get("路段编号") or row.get("清单编号") for row in linked],
                "异常": [dict(row) for row in bad],
            }
            if bad:
                result["一致"] = False
        return result

    def vehicle_positions(self) -> list[dict[str, Any]]:
        """车辆位置板：所有车辆 + 当前是否被某风雪任务占用及所在路段。"""
        positions = []
        for vehicle in store.rows("vehicle"):
            active = next(
                (lock for lock in store.rows(T_LOCK)
                 if lock.get("车辆id") == vehicle["id"] and lock.get("占用状态") == "占用中"),
                None,
            )
            positions.append({
                **vehicle,
                "占用中": active is not None,
                "所在事件": active.get("事件编号") if active else None,
                "所在路段": active.get("路段名称") if active else None,
                "任务编号": active.get("任务编号") if active else None,
            })
        return positions

    # =====================================================================
    # 事件创建与状态流转（严格顺序，禁止跳步/回退）
    # =====================================================================
    def create_event(self, values: dict[str, Any], op_no: str | None, path: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            name = str(values.get("事件名称") or "").strip()
            start = str(values.get("开始时间") or "").strip()
            end = str(values.get("结束时间") or "").strip()
            baseline = str(values.get("天气基准") or "").strip()
            missing = [label for label, v in (("事件名称", name), ("气象开始时间", start),
                                              ("气象结束时间", end), ("天气基准", baseline)) if not v]
            if missing:
                return _fail(f"缺少必填字段：{'、'.join(missing)}")
            if start >= end:
                return _fail("气象时段结束时间必须晚于开始时间，无法编排事件窗口")
            event = {
                "id": _next_id(T_EVENT),
                "事件编号": _next_no("FXSJ", "事件编号", T_EVENT),
                "事件名称": name,
                "开始时间": start,
                "结束时间": end,
                "天气基准": baseline,  # 冻结快照，历史作业始终按这一基准保留
                "预警级别": str(values.get("预警级别") or "蓝色"),
                "状态": EVENT_STATUSES[0],
                "创建时间": _now(),
                "pending": True,
                "abnormal": False,
            }
            store.rows(T_EVENT).append(event)

            for item in values.get("segments", []) or []:
                road_name = str(item.get("路段名称") or "").strip()
                if not road_name:
                    continue
                store.rows(T_SEGMENT).append({
                    "id": _next_id(T_SEGMENT),
                    "事件id": event["id"],
                    "事件编号": event["事件编号"],
                    "路段编号": str(item.get("路段编号") or "").strip() or _next_no("FKD", "路段编号", T_SEGMENT),
                    "路段名称": road_name,
                    "起止桩号": str(item.get("起止桩号") or "").strip(),
                    "是否封控": bool(item.get("是否封控", True)),
                    "阶段": EVENT_STATUSES[0],
                    "启动时间": None,
                    "收尾时间": None,
                    "关闭时间": None,
                    "阶段结论": "",
                    "阶段日志": [],
                })
            for item in values.get("materials", []) or []:
                material_name = str(item.get("材料名称") or "").strip()
                if not material_name:
                    continue
                try:
                    stock = float(item.get("库存量") or 0)
                except (TypeError, ValueError):
                    return _fail(f"材料「{material_name}」库存量必须是数字")
                store.rows(T_MATERIAL).append({
                    "id": _next_id(T_MATERIAL),
                    "清单编号": _next_no("WZQD", "清单编号", T_MATERIAL),
                    "事件id": event["id"],
                    "事件编号": event["事件编号"],
                    "材料编号": str(item.get("材料编号") or "").strip() or _next_no("WZ", "材料编号", T_MATERIAL),
                    "材料名称": material_name,
                    "单位": str(item.get("单位") or "吨"),
                    "库存量": stock,
                    "累计耗用": 0.0,
                    "备注": str(item.get("备注") or ""),
                    "变动日志": [{"时间": _now(), "说明": "随事件建立物资清单", "数量": stock}],
                })
            return {"ok": True, "message": f"风雪事件 {event['事件编号']} 已建立，天气基准已冻结", "event": event}

        return self._idempotent(op_no, path, values, apply)

    def _advance(
        self,
        row: dict[str, Any],
        action: str,
        rules: dict[str, int],
        status_field: str,
        label: str,
    ) -> str | None:
        """统一的顺序状态机：只能走到当前状态的下一个，已关闭拒绝一切动作。"""
        if action not in rules:
            return f"动作「{action}」不属于{label}可执行范围"
        current = row.get(status_field)
        if current == TERMINAL:
            return f"{label}已关闭，不能再执行任何动作（终态不可回退）"
        current_index = EVENT_STATUSES.index(current) if current in EVENT_STATUSES else -1
        target_index = rules[action]
        if target_index != current_index + 1:
            return f"{label}只能按 待启动 → 执行中 → 收尾 → 已关闭 顺序流转，当前为「{current}」，不能跳到「{EVENT_STATUSES[target_index]}」"
        return None

    def event_action(self, event_id: int, action: str, op_no: str | None, path: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            event = store.find(T_EVENT, event_id)
            if event is None:
                return _fail(f"风雪事件 {event_id} 不存在")
            error = self._advance(event, action, EVENT_ACTIONS, "状态", "风雪事件")
            if error:
                return _fail(error)
            target = EVENT_STATUSES[EVENT_ACTIONS[action]]
            if action == "关闭事件":
                open_segments = [s for s in self._segments(event_id) if s.get("阶段") != TERMINAL]
                running_tasks = [t for t in self._tasks(event_id) if t.get("任务状态") != "已完成"]
                blockers = []
                if open_segments:
                    blockers.append(f"{len(open_segments)} 个路段未关闭")
                if running_tasks:
                    blockers.append(f"{len(running_tasks)} 个任务未完成")
                if blockers:
                    return _fail("事件不能关闭：" + "、".join(blockers))
            event["状态"] = target
            event["pending"] = target != TERMINAL
            if target == TERMINAL:
                event["关闭时间"] = _now()
            return {"ok": True, "message": f"风雪事件已{action}，当前状态：{target}", "event": event}

        return self._idempotent(op_no, path, payload, apply)

    def segment_action(self, event_id: int, segment_id: int, action: str,
                       op_no: str | None, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            event = store.find(T_EVENT, event_id)
            if event is None:
                return _fail(f"风雪事件 {event_id} 不存在")
            segment = store.find(T_SEGMENT, segment_id)
            if segment is None or segment.get("事件id") != event_id:
                return _fail(f"路段 {segment_id} 不属于风雪事件 {event['事件编号']}")
            error = self._advance(segment, action, PHASE_ACTIONS, "阶段", "作业路段")
            if error:
                return _fail(error)
            if event.get("状态") == TERMINAL:
                return _fail("风雪事件已关闭，路段阶段不可再变更")
            if action == "启动路段" and event.get("状态") != "执行中":
                return _fail(f"事件当前为「{event.get('状态')}」，需先启动事件才能启动封控段")
            if action == "关闭路段":
                running = [
                    task for task in self._tasks(event_id)
                    if task.get("路段id") == segment_id and task.get("任务状态") != "已完成"
                ]
                if running:
                    return _fail(f"路段尚有 {len(running)} 个任务在执行，不能关闭")
            target = EVENT_STATUSES[PHASE_ACTIONS[action]]
            segment["阶段"] = target
            stamp_field = {1: "启动时间", 2: "收尾时间", 3: "关闭时间"}[PHASE_ACTIONS[action]]
            segment[stamp_field] = _now()
            conclusion = str(payload.get("调度结论") or "").strip()
            if conclusion:
                segment["阶段结论"] = conclusion
            segment.setdefault("阶段日志", []).append(
                {"动作": action, "时间": _now(), "结论": conclusion})
            return {"ok": True, "message": f"路段「{segment['路段名称']}」已{action}，阶段：{target}",
                    "segment": segment}

        return self._idempotent(op_no, path, payload, apply)

    # =====================================================================
    # 任务派发：任务/车辆/材料事务相符，防汛冲突按指挥令裁决
    # =====================================================================
    def dispatch(self, event_id: int, values: dict[str, Any], op_no: str | None, path: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            event = store.find(T_EVENT, event_id)
            if event is None:
                return _fail(f"风雪事件 {event_id} 不存在")
            if event.get("状态") == TERMINAL:
                return _fail("风雪事件已关闭，不能再派发任务")
            if event.get("状态") != "执行中":
                return _fail(f"事件当前为「{event.get('状态')}」，仅「执行中」事件可派发除雪任务")

            segment_id = values.get("路段id")
            segment = store.find(T_SEGMENT, int(segment_id)) if segment_id is not None else None
            if segment is None or segment.get("事件id") != event_id:
                return _fail("请选择本事件下的封控段后再调用材料与班组")
            if segment.get("阶段") != "执行中":
                return _fail(f"封控段当前阶段为「{segment.get('阶段')}」，仅执行中路段可直接调用班组")

            crew = str(values.get("作业班组") or "").strip()
            if not crew:
                return _fail("缺少作业班组，无法派发")
            vehicle_id = values.get("车辆id")
            vehicle = store.find("vehicle", int(vehicle_id)) if vehicle_id is not None else None
            if vehicle is None:
                return _fail("请选择作业车辆")
            if vehicle.get("status") != "在库":
                return _fail(f"车辆 {vehicle.get('车牌号')} 当前状态为「{vehicle.get('status')}」，不可占用")
            if any(lock.get("车辆id") == vehicle["id"] and lock.get("占用状态") == "占用中"
                   for lock in store.rows(T_LOCK)):
                return _fail(f"车辆 {vehicle.get('车牌号')} 已被其他任务占用")

            raw_items = values.get("材料") or []
            items: list[tuple[dict[str, Any], float]] = []
            for raw in raw_items:
                qty_raw = raw.get("数量")
                try:
                    qty = float(qty_raw)
                except (TypeError, ValueError):
                    return _fail("材料数量必须是数字")
                if qty <= 0:
                    continue
                material = next(
                    (row for row in store.rows(T_MATERIAL)
                     if row.get("事件id") == event_id and int(row["id"]) == int(raw.get("清单id"))),
                    None,
                )
                if material is None:
                    return _fail("物资清单中找不到所选材料，请从封控段的清单内调用")
                if float(material["库存量"]) < qty:
                    return _fail(
                        f"材料「{material['材料名称']}」库存不足：剩余 {material['库存量']}{material['单位']}，"
                        f"本次申请 {qty}{material['单位']}")
                items.append((material, qty))
            if not items:
                return _fail("至少调用一种材料且数量大于 0")

            # 除雪与防汛同时占用道路：必须有现行指挥令，并按令中优先方裁决
            order_no = ""
            conflicts = self._flood_conflicts(segment)
            if conflicts:
                order = self._active_order(str(segment.get("路段名称", "")))
                if order is None:
                    return _fail(
                        f"路段「{segment['路段名称']}」正被防汛响应（{conflicts[0].get('记录编号')}）同时占用，"
                        "须先下达应急指挥令确定优先级后再派发")
                if order.get("优先方") != "除雪":
                    return _fail(
                        f"应急指挥令 {order['令号']} 已确定防汛优先，除雪任务暂停派发；"
                        "如指挥令调整请重新下达")
                order_no = str(order["令号"])

            # 校验全部通过后才进事务：任务派发、车辆占用、材料扣减一次落地
            tables = [T_TASK, T_LOCK, T_LEDGER, T_MATERIAL, "vehicle"]
            snapshots = {name: copy.deepcopy(store.rows(name)) for name in tables}
            material_summary = "；".join(
                f"{m['材料名称']} {qty:g}{m['单位']}" for m, qty in items)

            def _commit() -> tuple[dict[str, Any], dict[str, Any]]:
                task = {
                    "id": _next_id(T_TASK),
                    "任务编号": _next_no("CXRW", "任务编号", T_TASK),
                    "事件id": event_id,
                    "事件编号": event["事件编号"],
                    "路段id": segment["id"],
                    "作业路段": segment["路段名称"],
                    "作业班组": crew,
                    "车辆id": vehicle["id"],
                    "车牌号": vehicle.get("车牌号"),
                    "材料": [{"清单id": m["id"], "材料名称": m["材料名称"], "数量": qty,
                             "单位": m["单位"]} for m, qty in items],
                    "材料摘要": material_summary,
                    "任务状态": "已派发",
                    "指挥令号": order_no,
                    "派发时间": _now(),
                    "开始时间": None,
                    "完成时间": None,
                }
                store.rows(T_TASK).append(task)
                store.rows(T_LOCK).append({
                    "id": _next_id(T_LOCK),
                    "事件id": event_id,
                    "事件编号": event["事件编号"],
                    "路段id": segment["id"],
                    "路段名称": segment["路段名称"],
                    "任务id": task["id"],
                    "任务编号": task["任务编号"],
                    "车辆id": vehicle["id"],
                    "车牌号": vehicle.get("车牌号"),
                    "占用状态": "占用中",
                    "占用起": _now(),
                    "占用止": None,
                })
                vehicle["status"] = "出车作业"
                for material, qty in items:
                    material["库存量"] = round(float(material["库存量"]) - qty, 3)
                    material["累计耗用"] = round(float(material.get("累计耗用", 0)) + qty, 3)
                    material.setdefault("变动日志", []).append({
                        "时间": _now(),
                        "说明": f"任务 {task['任务编号']} 派发扣减",
                        "数量": -qty,
                        "事件编号": event["事件编号"],
                    })
                # 除雪作业台账：落表即复制当时天气基准，历史作业不随后续天气变化改动
                ledger = {
                    "id": _next_id(T_LEDGER),
                    "台账编号": _next_no("CXTZ", "台账编号", T_LEDGER),
                    "事件id": event_id,
                    "事件编号": event["事件编号"],
                    "任务id": task["id"],
                    "任务编号": task["任务编号"],
                    "作业路段": segment["路段名称"],
                    "作业班组": crew,
                    "作业车辆": vehicle.get("车牌号"),
                    "材料明细": material_summary,
                    "天气基准": event["天气基准"],
                    "指挥令号": order_no,
                    "调度结论": "",
                    "作业状态": "执行中",
                    "作业时间": _now(),
                    "完成时间": None,
                }
                store.rows(T_LEDGER).append(ledger)
                return task, ledger

            try:
                task_row, ledger_row = self._transactional(snapshots, _commit)
            except Exception as exc:  # pragma: no cover - 防御性回滚
                return _fail(f"派发事务已回滚：{exc}")
            return {"ok": True,
                    "message": f"任务已派发到「{segment['路段名称']}」：车辆占用、材料扣减同步完成",
                    "task": task_row, "ledger": ledger_row}

        return self._idempotent(op_no, path, values, apply)

    def task_action(self, event_id: int, task_id: int, action: str, values: dict[str, Any],
                    op_no: str | None, path: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            event = store.find(T_EVENT, event_id)
            task = store.find(T_TASK, task_id)
            if event is None or task is None or task.get("事件id") != event_id:
                return _fail(f"任务 {task_id} 不属于风雪事件 {event_id}")
            if event.get("状态") == TERMINAL:
                return _fail("风雪事件已关闭，任务不可再变更")
            current = task.get("任务状态")
            target = {"开始作业": "执行中", "完成任务": "已完成"}.get(action)
            if target is None:
                return _fail(f"动作「{action}」不属于调度任务可执行范围")
            if current == "已完成":
                return _fail("任务已完成，不能重复执行（终态不可回退）")
            if action == "开始作业" and current != "已派发":
                return _fail(f"任务当前为「{current}」，不能开始作业")
            if action == "完成任务" and current not in ("已派发", "执行中"):
                return _fail(f"任务当前为「{current}」，不能完成")

            snapshots = {name: copy.deepcopy(store.rows(name))
                         for name in (T_TASK, T_LOCK, T_LEDGER, "vehicle")}
            try:
                def _commit() -> None:
                    task["任务状态"] = target
                    if target == "执行中":
                        task["开始时间"] = _now()
                    if target == "已完成":
                        task["完成时间"] = _now()
                        conclusion = str(values.get("调度结论") or "").strip()
                        if conclusion:
                            task["调度结论"] = conclusion
                        for lock in store.rows(T_LOCK):
                            if lock.get("任务id") == task["id"] and lock.get("占用状态") == "占用中":
                                lock["占用状态"] = "释放"
                                lock["占用止"] = _now()
                        vehicle = store.find("vehicle", int(task["车辆id"]))
                        if vehicle is not None and vehicle.get("status") == "出车作业":
                            vehicle["status"] = "在库"
                        ledger = next(
                            (row for row in store.rows(T_LEDGER)
                             if row.get("任务id") == task["id"] and row.get("事件id") == event_id),
                            None,
                        )
                        if ledger is not None:
                            ledger["作业状态"] = "已完成"
                            ledger["完成时间"] = _now()
                            if conclusion:
                                ledger["调度结论"] = conclusion
                self._transactional(snapshots, _commit)
            except Exception as exc:  # pragma: no cover
                return _fail(f"任务动作事务已回滚：{exc}")
            return {"ok": True, "message": f"任务 {task['任务编号']} 已{action}", "task": task}

        return self._idempotent(op_no, path, values, apply)

    # =====================================================================
    # 调度会话结论：一次写入台账、路段阶段、物资清单三处，同一事件引用
    # =====================================================================
    def add_conclusion(self, event_id: int, values: dict[str, Any],
                       op_no: str | None, path: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            event = store.find(T_EVENT, event_id)
            if event is None:
                return _fail(f"风雪事件 {event_id} 不存在")
            if event.get("状态") == TERMINAL:
                return _fail("风雪事件已关闭，调度会话不再补记结论")
            conclusion = str(values.get("调度结论") or "").strip()
            if not conclusion:
                return _fail("请填写调度会话结论")
            segment_id = values.get("路段id")
            task_id = values.get("任务id")
            segment = store.find(T_SEGMENT, int(segment_id)) if segment_id is not None else None
            if segment is None or segment.get("事件id") != event_id:
                return _fail("结论必须挂到本事件的作业路段上")
            task = store.find(T_TASK, int(task_id)) if task_id is not None else None
            if task is not None and task.get("事件id") != event_id:
                return _fail("任务不属于本事件，结论不能写入")

            snapshots = {name: copy.deepcopy(store.rows(name))
                         for name in (T_LEDGER, T_SEGMENT, T_MATERIAL, T_TASK)}
            refs: dict[str, Any] = {"事件id": event_id, "事件编号": event["事件编号"]}
            try:
                def _commit() -> None:
                    # 1) 除雪作业台账
                    ledger = None
                    if task is not None:
                        ledger = next(
                            (row for row in store.rows(T_LEDGER)
                             if row.get("任务id") == task["id"] and row.get("事件id") == event_id),
                            None,
                        )
                    if ledger is None:
                        ledger = {
                            "id": _next_id(T_LEDGER),
                            "台账编号": _next_no("CXTZ", "台账编号", T_LEDGER),
                            "事件id": event_id,
                            "事件编号": event["事件编号"],
                            "任务id": task["id"] if task else None,
                            "任务编号": task["任务编号"] if task else "",
                            "作业路段": segment["路段名称"],
                            "作业班组": task["作业班组"] if task else str(values.get("作业班组") or ""),
                            "作业车辆": task["车牌号"] if task else "",
                            "材料明细": task.get("材料摘要", "") if task else "",
                            "天气基准": event["天气基准"],
                            "作业状态": task["任务状态"] if task else "调度记录",
                            "作业时间": _now(),
                            "完成时间": None,
                        }
                        store.rows(T_LEDGER).append(ledger)
                    ledger["调度结论"] = conclusion
                    ledger["结论时间"] = _now()

                    # 2) 路段阶段
                    segment["阶段结论"] = conclusion
                    segment.setdefault("阶段日志", []).append(
                        {"动作": "调度会话结论", "时间": _now(),
                         "结论": conclusion, "事件编号": event["事件编号"]})
                    if task is not None:
                        task["调度结论"] = conclusion

                    # 3) 物资清单（该任务调用过的材料逐条留痕）
                    touched = []
                    material_ids = {item["清单id"] for item in (task or {}).get("材料", [])}
                    for material in store.rows(T_MATERIAL):
                        if material.get("事件id") == event_id and (
                            not material_ids or material["id"] in material_ids
                        ):
                            material.setdefault("变动日志", []).append({
                                "时间": _now(),
                                "说明": f"调度会话结论：{conclusion}",
                                "事件编号": event["事件编号"],
                            })
                            touched.append(material["清单编号"])
                    refs.update({
                        "台账编号": ledger["台账编号"],
                        "路段编号": segment["路段编号"],
                        "物资清单": touched,
                    })
                self._transactional(snapshots, _commit)
            except Exception as exc:  # pragma: no cover
                return _fail(f"结论写入事务已回滚：{exc}")
            return {"ok": True,
                    "message": "调度会话结论已同时写入除雪作业台账、路段阶段和物资清单（同一事件引用）",
                    "refs": refs}

        return self._idempotent(op_no, path, values, apply)

    # =====================================================================
    # 应急指挥令：除雪/防汛同占一路时的优先级依据
    # =====================================================================
    def list_orders(self) -> list[dict[str, Any]]:
        return sorted(store.rows(T_ORDER), key=lambda row: int(row["id"]), reverse=True)

    def create_order(self, values: dict[str, Any], op_no: str | None, path: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            road = str(values.get("影响路段") or "").strip()
            side = str(values.get("优先方") or "").strip()
            if not road:
                return _fail("缺少指挥令影响路段")
            if side not in ("除雪", "防汛"):
                return _fail("优先方必须是「除雪」或「防汛」")
            order = {
                "id": _next_id(T_ORDER),
                "令号": _next_no("ZHL", "令号", T_ORDER),
                "影响路段": road,
                "优先方": side,
                "事件类型": str(values.get("事件类型") or "除雪防汛并发"),
                "依据": str(values.get("依据") or "").strip(),
                "状态": "生效",
                "生效时间": _now(),
                "失效时间": None,
            }
            store.rows(T_ORDER).append(order)
            return {"ok": True, "message": f"应急指挥令 {order['令号']} 已下达，{side}优先", "order": order}

        return self._idempotent(op_no, path, values, apply)

    def revoke_order(self, order_id: int, op_no: str | None, path: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            order = store.find(T_ORDER, order_id)
            if order is None:
                return _fail(f"指挥令 {order_id} 不存在")
            if order.get("状态") != "生效":
                return _fail("指挥令已失效，不能重复撤销")
            order["状态"] = "撤销"
            order["失效时间"] = _now()
            return {"ok": True, "message": f"指挥令 {order['令号']} 已撤销", "order": order}

        return self._idempotent(op_no, path, payload, apply)

    # =====================================================================
    # 断网复位：按操作号幂等整单续传
    # =====================================================================
    def sync(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """复位后续传：每项 {操作号, path, payload}；已执行过的操作号直接回放，不重复扣料。"""
        results = []
        for index, op in enumerate(operations):
            op_no = str(op.get("操作号") or "").strip()
            path = str(op.get("path") or op.get("路径") or "").strip()
            payload = op.get("payload") or op.get("请求") or {}
            if not op_no:
                results.append({"序号": index, "ok": False, "message": "缺少操作号，已拒绝续传"})
                continue
            results.append(self._route_synced(op_no, path, payload))
        return {
            "ok": all(item.get("ok") for item in results),
            "message": f"续传完成：{sum(1 for item in results if item.get('ok'))}/{len(results)} 笔成功",
            "results": results,
        }

    def _route_synced(self, op_no: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """把排队时保存的规范化路径重新分发到对应写操作。"""
        import re

        try:
            stripped = path.replace("/api/snow", "")
            if stripped == "/events":
                return self.create_event(payload, op_no, path)
            if stripped == "/orders":
                return self.create_order(payload, op_no, path)

            seg_match = re.fullmatch(r"/events/(\d+)/segments/(\d+)/actions", stripped)
            if seg_match:
                return self.segment_action(int(seg_match.group(1)), int(seg_match.group(2)),
                                           str(payload.get("action") or ""), op_no, path, payload)
            event_match = re.fullmatch(r"/events/(\d+)/actions", stripped)
            if event_match:
                return self.event_action(int(event_match.group(1)),
                                         str(payload.get("action") or ""), op_no, path, payload)
            dispatch_match = re.fullmatch(r"/events/(\d+)/dispatches", stripped)
            if dispatch_match:
                return self.dispatch(int(dispatch_match.group(1)), payload, op_no, path)
            task_match = re.fullmatch(r"/events/(\d+)/tasks/(\d+)/actions", stripped)
            if task_match:
                return self.task_action(int(task_match.group(1)), int(task_match.group(2)),
                                        str(payload.get("action") or ""), payload, op_no, path)
            concl_match = re.fullmatch(r"/events/(\d+)/conclusions", stripped)
            if concl_match:
                return self.add_conclusion(int(concl_match.group(1)), payload, op_no, path)
            order_revoke = re.fullmatch(r"/orders/(\d+)/revoke", stripped)
            if order_revoke:
                return self.revoke_order(int(order_revoke.group(1)), op_no, path, payload)
            return {"ok": False, "操作号": op_no, "message": f"无法识别的续传路径：{path}"}
        except Exception as exc:  # pragma: no cover - 单笔失败不影响队列其余操作
            return {"ok": False, "操作号": op_no, "message": f"续传失败：{exc}"}


service = SnowCommandService()
