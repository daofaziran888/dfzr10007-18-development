"""数据准备流水线计划与完成水位的回归测试。

覆盖需求要点：
- 按输入源、转换步骤、依赖关系、输出目标生成逐任务运行状态；
- 重复启动同一准备任务只保留一个运行中实例；
- 两个依赖任务争抢同一输出目标时只允许一个提交（先到者优先）；
- 失败任务保留断点，后续步骤不得越过失败依赖继续执行；
- 完成水位交给数据定时刷新读取，上游异常恢复后只继续未完成下游；
- 依赖失败时停在待处理并说明原因，可重试；没有可运行任务时计划为空。

运行方式：cd backend && .venv/bin/python -m unittest tests.test_etl_pipeline -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SEED_ROWS  # noqa: E402
from app.services.etl import EtlService  # noqa: E402
from app.services.refresh import RefreshService  # noqa: E402
from app.store import store  # noqa: E402


def reset_store() -> None:
    for module in ("etl", "refresh"):
        store._tables[module] = [dict(row) for row in SEED_ROWS[module]]
    store._watermarks.clear()


class EtlPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.etl = EtlService()
        self.refresh = RefreshService()

    def entry_id(self, code: str) -> int:
        for row in store.rows("etl"):
            if row.get("任务编号") == code:
                return int(row["id"])
        raise AssertionError(f"任务 {code} 不存在")

    def test_seed_checklist_matches_etl_0001_to_0003(self) -> None:
        """用 ETL-0001 至 ETL-0003 核对任务名称、输入源、转换步骤与输出目标。"""
        rows = {row["任务编号"]: row for row in store.rows("etl")}
        self.assertEqual(rows["ETL-0001"]["任务名称"], "订单明细每日清洗")
        self.assertEqual(rows["ETL-0001"]["输入源"], "ods.orders_raw")
        self.assertEqual(rows["ETL-0001"]["转换步骤"], "数据去重→空值过滤→金额单位标准化")
        self.assertEqual(rows["ETL-0001"]["输出目标"], "dwd.orders_clean")
        self.assertEqual(rows["ETL-0002"]["依赖关系"], "ETL-0001")
        self.assertEqual(rows["ETL-0002"]["输出目标"], "dws.user_daily_summary")
        self.assertEqual(rows["ETL-0003"]["依赖关系"], "ETL-0002")
        self.assertEqual(rows["ETL-0003"]["输出目标"], "ads.sales_dashboard")

    def test_plan_orders_by_dependency(self) -> None:
        plan = self.etl.plan()
        self.assertEqual([item["任务编号"] for item in plan], ["ETL-0001", "ETL-0002", "ETL-0003"])
        self.assertTrue(all(item["运行状态"] == "待处理" for item in plan))
        self.assertEqual(plan[0]["断点"], "0/3")

    def test_plan_empty_when_no_tasks(self) -> None:
        store._tables["etl"] = []
        self.assertEqual(self.etl.plan(), [])

    def test_dependency_not_finished_blocks_start(self) -> None:
        entry, message = self.etl.run_action(self.entry_id("ETL-0002"), "启动任务")
        self.assertIsNone(entry)
        self.assertIn("ETL-0001", message)
        row = store.find("etl", self.entry_id("ETL-0002"))
        self.assertEqual(row["status"], "待处理")
        self.assertIn("ETL-0001", row["阻塞原因"])

    def test_duplicate_running_instance_rejected(self) -> None:
        # 同一任务编号登记两条（绕过登记校验，模拟历史重复数据），只允许一个运行中。
        dup = dict(store.find("etl", self.entry_id("ETL-0001")))
        dup["id"] = 99
        store.rows("etl").append(dup)
        first, _ = self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.assertIsNotNone(first)
        second, message = self.etl.run_action(99, "启动任务")
        self.assertIsNone(second)
        self.assertIn("运行中实例", message)
        running = [row for row in store.rows("etl") if row.get("任务编号") == "ETL-0001" and row.get("status") == "运行中"]
        self.assertEqual(len(running), 1)

    def test_output_target_conflict_first_comer_wins(self) -> None:
        self.etl.create_entry({
            "任务编号": "ETL-0009",
            "任务名称": "订单清洗竞争任务",
            "输入源": "ods.orders_raw",
            "转换步骤": "数据去重",
            "输出目标": "dwd.orders_clean",
        })
        first, _ = self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.assertIsNotNone(first)
        second, message = self.etl.run_action(self.entry_id("ETL-0009"), "启动任务")
        self.assertIsNone(second)
        self.assertIn("先到者优先", message)
        loser = store.find("etl", self.entry_id("ETL-0009"))
        self.assertEqual(loser["status"], "待处理")
        self.assertIn("ETL-0001", loser["阻塞原因"])

    def test_failure_keeps_checkpoint_and_blocks_downstream(self) -> None:
        self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        entry, message = self.etl.run_action(self.entry_id("ETL-0001"), "标记失败", {"断点步骤": 2})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["断点步骤"], 2)
        self.assertEqual(entry["status"], "失败")
        downstream, reason = self.etl.run_action(self.entry_id("ETL-0002"), "启动任务")
        self.assertIsNone(downstream)
        self.assertIn("失败", reason)
        row = store.find("etl", self.entry_id("ETL-0002"))
        self.assertEqual(row["status"], "待处理")

    def test_retry_resumes_from_checkpoint(self) -> None:
        self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.etl.run_action(self.entry_id("ETL-0001"), "标记失败", {"断点步骤": 2})
        entry, message = self.etl.run_action(self.entry_id("ETL-0001"), "重试任务")
        self.assertIsNotNone(entry)
        self.assertIn("2/3", message)
        self.assertEqual(entry["status"], "运行中")

    def test_completed_task_cannot_restart(self) -> None:
        self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.etl.run_action(self.entry_id("ETL-0001"), "完成任务")
        entry, message = self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.assertIsNone(entry)
        self.assertIn("已完成", message)

    def test_completion_publishes_watermark_for_refresh(self) -> None:
        self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        _, message = self.etl.run_action(self.entry_id("ETL-0001"), "完成任务", {"水位": "2026-11-14"})
        self.assertIn("2026-11-14", message)
        watermark = store.get_watermark("dwd.orders_clean")
        self.assertIsNotNone(watermark)
        self.assertEqual(watermark["增量字段"], "updated_at")
        self.assertEqual(watermark["来源任务"], "ETL-0001")
        # 数据定时刷新读取水位，从增量字段继续。
        rows, _ = self.refresh.list_entries()
        refr = next(row for row in rows if row["策略编号"] == "REFR-0001")
        self.assertEqual(refr["上游水位"], "2026-11-14")
        entry, refresh_message = self.refresh.run_action(1, "执行刷新")
        self.assertIsNotNone(entry)
        self.assertIn("updated_at", refresh_message)
        self.assertEqual(entry["上次刷新水位"], "2026-11-14")

    def test_refresh_waits_when_upstream_failed_and_recovers(self) -> None:
        self.etl.run_action(self.entry_id("ETL-0001"), "启动任务")
        self.etl.run_action(self.entry_id("ETL-0001"), "标记失败")
        entry, message = self.refresh.run_action(1, "执行刷新")
        self.assertIsNone(entry)
        self.assertIn("停在待处理", message)
        # 上游恢复：重试并完成，只继续未完成下游，刷新即可继续。
        self.etl.run_action(self.entry_id("ETL-0001"), "重试任务")
        self.etl.run_action(self.entry_id("ETL-0001"), "完成任务")
        entry, message = self.refresh.run_action(1, "执行刷新")
        self.assertIsNotNone(entry)
        self.assertIn("完成水位", message)

    def test_full_chain_unblocks_downstream_one_by_one(self) -> None:
        for code in ("ETL-0001", "ETL-0002", "ETL-0003"):
            entry, _ = self.etl.run_action(self.entry_id(code), "启动任务")
            self.assertIsNotNone(entry, f"{code} 应可启动")
            entry, _ = self.etl.run_action(self.entry_id(code), "完成任务")
            self.assertIsNotNone(entry, f"{code} 应可完成")
        plan = self.etl.plan()
        self.assertTrue(all(item["运行状态"] == "已完成" for item in plan))
        self.assertEqual(len(store.watermarks()), 3)


if __name__ == "__main__":
    unittest.main()
