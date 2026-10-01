"""风雪事件指挥板规则回归测试：状态机、事务、指挥令、三处引用、幂等续传。

运行：cd backend && python3 -m unittest app.tests.test_snow_command -v
"""
from __future__ import annotations

import copy
import unittest

from app import seed
from app.services.snow_command import service, store


class SnowCommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例一份全新的内存仓库，避免用例间串状态
        store._tables = {
            name: [dict(row) for row in rows]
            for name, rows in copy.deepcopy(seed.SEED_ROWS).items()
        }

    # ---------- 状态机 ----------
    def test_event_status_must_advance_one_step(self) -> None:
        result = service.create_event({
            "事件名称": "测试", "开始时间": "2026-10-03 08:00",
            "结束时间": "2026-10-04 08:00", "天气基准": "小雪",
        }, "op-1", "/api/snow/events")
        event_id = result["event"]["id"]
        # 待启动不能跳到收尾/关闭
        for action in ("转入收尾", "关闭事件"):
            r = service.event_action(event_id, action, f"op-jump-{action}", "/x", {})
            self.assertFalse(r["ok"])
            self.assertIn("顺序流转", r["message"])
        # 正确顺序一路走到关闭
        for action in ("启动事件", "转入收尾", "关闭事件"):
            r = service.event_action(event_id, action, f"op-flow-{action}", "/x", {})
            self.assertTrue(r["ok"], r.get("message"))
        # 已关闭不能再做任何动作（含启动）
        r = service.event_action(event_id, "启动事件", "op-reopen", "/x", {})
        self.assertFalse(r["ok"])
        self.assertIn("终态", r["message"])

    def test_segment_needs_event_started(self) -> None:
        # 待启动事件下的待启动路段不能启动路段
        created = service.create_event({
            "事件名称": "待启动事件", "开始时间": "2026-10-03 08:00",
            "结束时间": "2026-10-04 08:00", "天气基准": "小雪",
            "segments": [{"路段名称": "测试路"}],
        }, "op-new", "/x")
        new_event_id = created["event"]["id"]
        new_segment = next(row for row in store.rows("snow_segment")
                           if row["事件id"] == new_event_id)
        r = service.segment_action(new_event_id, new_segment["id"], "启动路段",
                                   "op-event-not-open", "/x", {})
        self.assertFalse(r["ok"])
        self.assertIn("先启动事件", r["message"])

    def test_event_close_blocked_by_open_segments_and_tasks(self) -> None:
        # 事件1 执行中：转收尾后仍有未关闭路段和在途任务，关闭被拦
        self.assertTrue(service.event_action(1, "转入收尾", "op-wrap", "/x", {})["ok"])
        r = service.event_action(1, "关闭事件", "op-close", "/x", {})
        self.assertFalse(r["ok"])
        self.assertIn("事件不能关闭", r["message"])

    def test_segment_status_follows_same_machine_and_needs_open_event(self) -> None:
        # 待启动路段不能跳到收尾（跳步）
        r = service.segment_action(1, 3, "路段收尾", "op-seg-jump", "/x", {})
        self.assertFalse(r["ok"])
        self.assertIn("顺序流转", r["message"])
        # 执行中事件内，待启动路段可正常启动；随后不能回到启动动作
        r = service.segment_action(1, 3, "启动路段", "op-seg-start", "/x", {})
        self.assertTrue(r["ok"], r.get("message"))
        r = service.segment_action(1, 3, "启动路段", "op-seg-restart", "/x", {})
        self.assertFalse(r["ok"])

    # ---------- 事务：派发/占用/扣减 ----------
    def test_dispatch_is_transactional_and_writes_three_places(self) -> None:
        materials = {row["id"]: row["库存量"] for row in store.rows("snow_material")}
        # 库存不足：任务、锁、台账、车辆、库存全部不动
        r = service.dispatch(1, {
            "路段id": 1, "作业班组": "测试班", "车辆id": 1,
            "材料": [{"清单id": 3, "数量": 999}],
        }, "op-fail", "/api/snow/events/1/dispatches")
        self.assertFalse(r["ok"])
        self.assertIn("库存不足", r["message"])
        self.assertEqual(
            {row["id"]: row["库存量"] for row in store.rows("snow_material")}, materials)
        vehicle = store.find("vehicle", 1)
        self.assertEqual(vehicle["status"], "在库")
        task_count = len(store.rows("snow_dispatch"))
        ledger_count = len(store.rows("snow_ledger"))

        # 正常派发：事务同时落地
        r = service.dispatch(1, {
            "路段id": 1, "作业班组": "测试班", "车辆id": 1,
            "材料": [{"清单id": 3, "数量": 2}],
        }, "op-ok", "/api/snow/events/1/dispatches")
        self.assertTrue(r["ok"], r.get("message"))
        self.assertEqual(len(store.rows("snow_dispatch")), task_count + 1)
        self.assertEqual(len(store.rows("snow_ledger")), ledger_count + 1)
        self.assertEqual(store.find("vehicle", 1)["status"], "出车作业")
        self.assertEqual(store.find("snow_material", 3)["库存量"], 16.0)
        # 台账落表时即冻结天气基准（历史作业保留口径）
        self.assertIn("暴雪橙色预警", r["ledger"]["天气基准"])
        # 三处引用同一事件
        self.assertEqual(r["task"]["事件id"], r["ledger"]["事件id"])
        refs = service.references(1)
        self.assertTrue(refs["一致"])

    def test_occupied_vehicle_cannot_be_double_booked(self) -> None:
        r = service.dispatch(1, {
            "路段id": 1, "作业班组": "测试班", "车辆id": 2,
            "材料": [{"清单id": 3, "数量": 1}],
        }, "op-busy", "/api/snow/events/1/dispatches")
        self.assertFalse(r["ok"])
        self.assertTrue("已被其他任务占用" in r["message"] or "不可占用" in r["message"],
                        r["message"])

    # ---------- 指挥令优先级 ----------
    def test_flood_conflict_requires_order_and_respects_priority(self) -> None:
        # 种子数据：滨海大道有响应中的防汛记录，现行令 ZHL-0001 防汛优先
        r = service.dispatch(1, {
            "路段id": 2, "作业班组": "测试班", "车辆id": 1,
            "材料": [{"清单id": 1, "数量": 1}],
        }, "op-flood", "/x")
        self.assertFalse(r["ok"])
        self.assertIn("防汛优先", r["message"])
        # 撤销令后：同占一路且无令，仍然拦截
        service.revoke_order(1, "op-revoke", "/x", {})
        r = service.dispatch(1, {
            "路段id": 2, "作业班组": "测试班", "车辆id": 1,
            "材料": [{"清单id": 1, "数量": 1}],
        }, "op-noorder", "/x")
        self.assertFalse(r["ok"])
        self.assertIn("应急指挥令", r["message"])
        # 下达除雪优先令后放行
        order = service.create_order(
            {"影响路段": "滨海大道（跨海大桥引线段）", "优先方": "除雪", "依据": "测试"},
            "op-snow-order", "/x")
        r = service.dispatch(1, {
            "路段id": 2, "作业班组": "测试班", "车辆id": 1,
            "材料": [{"清单id": 1, "数量": 1}],
        }, "op-snow-ok", "/x")
        self.assertTrue(r["ok"], r.get("message"))
        self.assertEqual(r["task"]["指挥令号"], order["order"]["令号"])

    # ---------- 结论写三处 ----------
    def test_conclusion_lands_on_ledger_segment_and_materials(self) -> None:
        r = service.add_conclusion(1, {
            "路段id": 1, "任务id": 1, "调度结论": "22时加撒一次",
        }, "op-conclusion", "/x")
        self.assertTrue(r["ok"], r.get("message"))
        self.assertEqual(r["refs"]["事件id"], 1)
        segment = store.find("snow_segment", 1)
        self.assertIn("22时加撒一次", segment["阶段结论"])
        ledger = next(row for row in store.rows("snow_ledger") if row["任务id"] == 1)
        self.assertEqual(ledger["调度结论"], "22时加撒一次")
        for material in store.rows("snow_material"):
            if material["id"] in (1, 2):  # 任务 CXRW-0001 调用的两种材料
                self.assertTrue(any("22时加撒一次" in e["说明"]
                                    for e in material["变动日志"]))

    # ---------- 幂等与断网续传 ----------
    def test_idempotent_op_no_never_double_deducts(self) -> None:
        payload = {"路段id": 1, "作业班组": "测试班", "车辆id": 1,
                   "材料": [{"清单id": 3, "数量": 2}]}
        first = service.dispatch(1, payload, "dup-op", "/x")
        self.assertTrue(first["ok"])
        second = service.dispatch(1, payload, "dup-op", "/x")
        self.assertTrue(second.get("幂等回放"))
        self.assertEqual(second["task"]["id"], first["task"]["id"])
        # 只扣了一次 2 件：18 → 16
        self.assertEqual(store.find("snow_material", 3)["库存量"], 16.0)

    def test_sync_replays_queue_in_order_and_reports_failures(self) -> None:
        new_event_payload = {
            "事件名称": "离线新建", "开始时间": "2026-10-05 08:00",
            "结束时间": "2026-10-06 08:00", "天气基准": "小雪蓝色",
        }
        result = service.sync([
            {"操作号": "q-1", "path": "/api/snow/events/1/segments/1/actions",
             "payload": {"action": "路段收尾", "调度结论": "离线记录"}},
            {"操作号": "q-2", "path": "/api/snow/events", "payload": new_event_payload},
            {"操作号": "q-3", "path": "/api/snow/unknown", "payload": {}},
        ])
        self.assertFalse(result["ok"])  # 有一笔路径非法，但不影响前两笔
        self.assertTrue(result["results"][0]["ok"])
        self.assertTrue(result["results"][1]["ok"])
        self.assertFalse(result["results"][2]["ok"])
        self.assertEqual(store.find("snow_segment", 1)["阶段"], "收尾")
        # 整单二次续传：已执行操作号全部回放，不重复流转
        again = service.sync([
            {"操作号": "q-1", "path": "/api/snow/events/1/segments/1/actions",
             "payload": {"action": "路段收尾"}},
        ])
        self.assertTrue(again["results"][0].get("幂等回放"))
        self.assertEqual(store.find("snow_segment", 1)["阶段"], "收尾")

    def test_closed_event_rejects_every_mutation(self) -> None:
        # 历史事件 2 已关闭：结论补记、任务派发都拒绝
        self.assertFalse(service.add_conclusion(
            2, {"路段id": 4, "调度结论": "改历史"}, "op-h1", "/x")["ok"])
        self.assertFalse(service.dispatch(
            2, {"路段id": 4, "作业班组": "x", "车辆id": 1, "材料": []}, "op-h2", "/x")["ok"])


if __name__ == "__main__":
    unittest.main()
