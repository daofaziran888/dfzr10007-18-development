"""数据准备流水线业务规则：计划生成、依赖裁决、断点与完成水位都收在这里。

设计约定：
- 状态序列：待处理 → 运行中 → 已完成 / 失败 / 已暂停 → 已归档。
- 同一任务编号同一时间只允许一个运行中实例，重复启动会被拦下。
- 两个任务争抢同一输出目标时先到者优先，后到者停在待处理并说明占用者。
- 失败任务保留断点（已完成的转换步骤数），重试时从断点继续；
  依赖未完成的任务一律停在待处理，后续步骤不得越过失败依赖继续执行。
- 任务完成时把完成水位（增量字段 + 水位值）写入共享水位表，
  数据定时刷新模块读取该水位决定从哪个增量字段继续。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.store import store

MODULE = "etl"
REQUIRED_FIELDS = ["任务编号", "任务名称", "输入源", "转换步骤", "输出目标"]
STATUS_ORDER = ["待处理", "运行中", "已暂停", "已完成", "失败", "已归档"]
TERMINAL_STATUSES = {"已完成", "已归档"}

# 动作 → (允许的前置状态, 目标状态)；进入「运行中」的动作要先过依赖与资源裁决。
ACTION_RULES: dict[str, tuple[set[str], str]] = {
    "启动任务": ({"待处理", "已暂停"}, "运行中"),
    "重试任务": ({"失败", "待处理"}, "运行中"),
    "完成任务": ({"运行中"}, "已完成"),
    "标记失败": ({"运行中"}, "失败"),
    "暂停任务": ({"运行中"}, "已暂停"),
    "归档任务": ({"已完成", "已暂停", "失败"}, "已归档"),
}
NO_DEPENDENCY = {"无", "-", "—", ""}


def _split_steps(entry: dict[str, Any]) -> list[str]:
    """把转换步骤拆成有序步骤列表，用于计算断点位置。"""
    raw = str(entry.get("转换步骤") or "")
    return [part.strip() for part in raw.split("→") if part.strip()]


def _parse_dependencies(entry: dict[str, Any]) -> list[str]:
    """解析依赖关系字段：支持逗号、顿号、分号分隔的任务编号列表。"""
    raw = str(entry.get("依赖关系") or "").strip()
    if raw in NO_DEPENDENCY:
        return []
    return [part for part in re.split(r"[,，、;；\s]+", raw) if part and part not in NO_DEPENDENCY]


class EtlService:
    # ---------- 查询 ----------

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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def plan(self) -> list[dict[str, Any]]:
        """生成流水线计划：按依赖关系拓扑排序，逐任务给出运行状态。

        每个计划项包含输入源、转换步骤、输出目标、依赖关系、运行状态、
        断点、阻塞原因与完成水位；没有任务时返回空列表，由前端展示暂无数据。
        """
        rows = store.rows(MODULE)
        by_code = {str(row.get("任务编号")): row for row in rows}
        indegree: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for row in rows:
            code = str(row.get("任务编号"))
            deps = [dep for dep in _parse_dependencies(row) if dep in by_code]
            indegree[code] = len(deps)
            for dep in deps:
                dependents.setdefault(dep, []).append(code)
        queue = sorted(code for code, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while queue:
            code = queue.pop(0)
            ordered.append(code)
            for child in sorted(dependents.get(code, [])):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
                    queue.sort()
        # 依赖成环的任务排不进拓扑序列，附在末尾并标注原因。
        cyclic = [code for code, degree in indegree.items() if degree > 0]
        items: list[dict[str, Any]] = []
        for code in ordered + sorted(cyclic):
            row = by_code[code]
            steps = _split_steps(row)
            done = min(int(row.get("断点步骤") or 0), len(steps))
            reason = str(row.get("阻塞原因") or "")
            if code in cyclic:
                reason = reason or "依赖关系存在循环，无法进入可执行序列"
            items.append({
                "id": row.get("id"),
                "任务编号": code,
                "任务名称": row.get("任务名称"),
                "输入源": row.get("输入源"),
                "转换步骤": row.get("转换步骤"),
                "输出目标": row.get("输出目标"),
                "依赖关系": row.get("依赖关系"),
                "运行状态": row.get("status"),
                "断点": f"{done}/{len(steps)}",
                "阻塞原因": reason,
                "增量字段": row.get("增量字段"),
                "完成水位": row.get("完成水位") or "",
                "可执行动作": self._available_actions(row),
            })
        return items

    def watermarks(self) -> list[dict[str, Any]]:
        """完成水位清单：交给数据定时刷新读取。"""
        return store.watermarks()

    # ---------- 登记 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        code = str(values.get("任务编号")).strip()
        if any(str(row.get("任务编号")) == code for row in rows):
            return None, [f"任务编号 {code} 已存在，同一准备任务只允许登记一次"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: str(values.get(field)).strip() for field in REQUIRED_FIELDS})
        entry["调度周期"] = str(values.get("调度周期") or "每日 02:00")
        entry["依赖关系"] = str(values.get("依赖关系") or "无")
        entry["增量字段"] = str(values.get("增量字段") or "updated_at")
        entry["断点步骤"] = 0
        entry["完成水位"] = ""
        entry["阻塞原因"] = ""
        entry["任务状态"] = STATUS_ORDER[0]
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ---------- 动作 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"准备任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据准备流水线可执行范围"
        allowed, target = ACTION_RULES[action]
        current = str(entry.get("status"))
        if current not in allowed:
            return None, f"任务当前状态为「{current}」，不允许执行「{action}」"
        params = params or {}
        if target == "运行中":
            reason = self._check_runnable(entry)
            if reason:
                # 依赖未满足或输出目标被抢占：停在待处理并说明原因，用户可重试。
                self._set_status(entry, "待处理", reason=reason)
                return None, f"任务停在待处理：{reason}"
            entry["阻塞原因"] = ""
        if action == "完成任务":
            conflict = self._output_lock_holder(entry)
            if conflict is not None:
                return None, (
                    f"输出目标 {entry.get('输出目标')} 正被任务 {conflict} 占用，"
                    "本次提交被拦截，只允许一个任务提交"
                )
        self._set_status(entry, target)
        if action == "完成任务":
            message = self._complete(entry, params)
        elif action == "标记失败":
            message = self._fail(entry, params)
        elif action == "重试任务":
            message = f"准备任务已从断点继续（断点 {self._checkpoint_text(entry)}）"
        else:
            message = f"准备任务已{action}"
        return entry, message

    # ---------- 内部规则 ----------

    def _available_actions(self, entry: dict[str, Any]) -> list[str]:
        current = str(entry.get("status"))
        return [action for action, (allowed, _target) in ACTION_RULES.items() if current in allowed]

    def _set_status(self, entry: dict[str, Any], status: str, *, reason: str = "") -> None:
        entry["status"] = status
        entry["任务状态"] = status
        entry["pending"] = status not in TERMINAL_STATUSES
        entry["abnormal"] = status == "失败"
        if reason:
            entry["阻塞原因"] = reason
        elif status == "运行中":
            entry["阻塞原因"] = ""

    def _checkpoint_text(self, entry: dict[str, Any]) -> str:
        steps = _split_steps(entry)
        done = min(int(entry.get("断点步骤") or 0), len(steps))
        return f"{done}/{len(steps)}"

    def _output_lock_holder(self, entry: dict[str, Any]) -> str | None:
        """输出目标冲突裁决：先到者优先，返回持有同一输出目标的运行中任务编号。"""
        target = str(entry.get("输出目标") or "")
        if not target:
            return None
        for row in store.rows(MODULE):
            if row is entry:
                continue
            if row.get("status") == "运行中" and str(row.get("输出目标") or "") == target:
                return str(row.get("任务编号"))
        return None

    def _check_runnable(self, entry: dict[str, Any]) -> str | None:
        """进入运行中之前的裁决：实例唯一、依赖完成、输出目标未被抢占。"""
        code = str(entry.get("任务编号"))
        for row in store.rows(MODULE):
            if row is not entry and row.get("status") == "运行中" and str(row.get("任务编号")) == code:
                return f"任务 {code} 已有一个运行中实例，重复启动只保留一个"
        rows_by_code = {str(row.get("任务编号")): row for row in store.rows(MODULE)}
        for dep in _parse_dependencies(entry):
            dep_row = rows_by_code.get(dep)
            if dep_row is None:
                return f"依赖任务 {dep} 不存在，请核对依赖关系"
            dep_status = str(dep_row.get("status"))
            if dep_status == "失败":
                return f"依赖任务 {dep} 执行失败，后续步骤不得越过失败依赖继续执行"
            if dep_status != "已完成":
                return f"依赖任务 {dep} 尚未完成（当前 {dep_status}）"
        holder = self._output_lock_holder(entry)
        if holder is not None:
            return f"输出目标 {entry.get('输出目标')} 已被任务 {holder} 占用，先到者优先"
        return None

    def _complete(self, entry: dict[str, Any], params: dict[str, Any]) -> str:
        steps = _split_steps(entry)
        entry["断点步骤"] = len(steps)
        watermark = str(params.get("水位") or date.today().isoformat())
        entry["完成水位"] = watermark
        record = {
            "增量字段": entry.get("增量字段") or "updated_at",
            "水位值": watermark,
            "来源任务": entry.get("任务编号"),
            "完成时间": date.today().isoformat(),
        }
        store.set_watermark(str(entry.get("输出目标")), record)
        return (
            f"准备任务已完成，完成水位 {watermark}（增量字段 {record['增量字段']}）"
            "已交给数据定时刷新"
        )

    def _fail(self, entry: dict[str, Any], params: dict[str, Any]) -> str:
        steps = _split_steps(entry)
        current = int(entry.get("断点步骤") or 0)
        try:
            failed_at = int(params.get("断点步骤"))
        except (TypeError, ValueError):
            failed_at = max(current, 1)
        entry["断点步骤"] = min(failed_at, len(steps))
        entry["阻塞原因"] = f"执行失败，断点保留在第 {entry['断点步骤']} 步，可重试"
        return f"准备任务执行失败，断点已保留（{self._checkpoint_text(entry)}），修复后可重试"
