"""数据定时刷新业务规则：状态流转、字段校验与筛选口径都收在这里。

刷新策略不再自己猜增量起点：执行刷新时读取数据准备流水线登记的完成水位
（按目标数据集匹配），从水位对应的增量字段继续；上游准备任务失败或尚无
水位时，刷新停在待处理并说明原因，上游恢复后重试即可。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "refresh"
ETL_MODULE = "etl"
REQUIRED_FIELDS = ["策略编号", "目标数据集", "刷新周期"]
STATUS_ORDER = ["已创建", "生效中", "已暂停", "已关闭"]

# 动作 → (允许的前置状态, 目标状态)；执行刷新不改变状态，只推进水位。
ACTION_RULES: dict[str, tuple[set[str], str]] = {
    "启用刷新": ({"已创建", "已暂停"}, "生效中"),
    "暂停刷新": ({"生效中"}, "已暂停"),
    "关闭刷新": ({"已暂停"}, "已关闭"),
    "执行刷新": ({"生效中"}, "生效中"),
}


def _upstream_failure(target_dataset: str) -> str | None:
    """检查目标数据集对应的上游准备任务是否处于失败状态。"""
    for row in store.rows(ETL_MODULE):
        if str(row.get("输出目标") or "") == target_dataset and row.get("status") == "失败":
            return str(row.get("任务编号"))
    return None


class RefreshService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_watermark(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._with_watermark(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["增量字段"] = str(values.get("增量字段") or "updated_at")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"刷新策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据定时刷新可执行范围"
        allowed, target = ACTION_RULES[action]
        current = str(entry.get("status"))
        if current not in allowed:
            return None, f"刷新策略当前状态为「{current}」，不允许执行「{action}」"
        if action == "执行刷新":
            return self._run_refresh(entry)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = False
        return entry, f"刷新策略已{action}"

    # ---------- 内部规则 ----------

    def _with_watermark(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给列表/详情附加上游完成水位，刷新侧据此知道从哪个增量字段继续。"""
        row = dict(entry)
        watermark = store.get_watermark(str(entry.get("目标数据集") or ""))
        if watermark:
            row["上游水位"] = watermark.get("水位值", "")
            row["水位增量字段"] = watermark.get("增量字段", "")
            row["水位来源任务"] = watermark.get("来源任务", "")
        else:
            row["上游水位"] = ""
            row["水位增量字段"] = ""
            row["水位来源任务"] = ""
        failed = _upstream_failure(str(entry.get("目标数据集") or ""))
        row["上游异常"] = f"上游任务 {failed} 失败" if failed else ""
        return row

    def _run_refresh(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        target_dataset = str(entry.get("目标数据集") or "")
        failed = _upstream_failure(target_dataset)
        if failed is not None:
            entry["pending"] = True
            return None, (
                f"上游准备任务 {failed} 执行失败，刷新停在待处理；"
                "待上游异常恢复后重试，只继续未完成部分"
            )
        watermark = store.get_watermark(target_dataset)
        if watermark is None:
            entry["pending"] = True
            return None, f"目标数据集 {target_dataset} 暂无完成水位，上游准备任务尚未完成，刷新停在待处理"
        incremental_field = str(watermark.get("增量字段") or entry.get("增量字段") or "updated_at")
        entry["增量字段"] = incremental_field
        entry["上次刷新水位"] = watermark.get("水位值", "")
        entry["下一次刷新"] = (date.today() + timedelta(days=1)).isoformat()
        entry["pending"] = True
        entry["abnormal"] = False
        return entry, (
            f"刷新已执行：从增量字段 {incremental_field} 的完成水位 "
            f"{watermark.get('水位值', '')}（来源任务 {watermark.get('来源任务', '')}）继续"
        )
