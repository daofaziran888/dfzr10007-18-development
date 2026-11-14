"""数据准备流水线接口：维护准备任务，并暴露逐任务运行状态与完成水位。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.etl import EtlService

router = APIRouter(prefix="/api/etl", tags=["数据准备流水线"])

service = EtlService()

LIST_FIELDS = ["任务编号", "任务名称", "输入源", "转换步骤", "输出目标", "调度周期", "依赖关系", "任务状态"]
STATUSES = ["待处理", "运行中", "已暂停", "已完成", "失败", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待处理、运行中、已暂停、已完成、失败、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤数据准备流水线列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/plan")
def get_plan() -> dict[str, Any]:
    """流水线计划：按依赖关系排序的逐任务运行状态；没有可运行任务时返回空列表。"""
    items = service.plan()
    return {"items": items, "total": len(items)}


@router.get("/watermarks")
def get_watermarks() -> dict[str, Any]:
    """完成水位清单：数据定时刷新据此决定从哪个增量字段继续。"""
    items = service.watermarks()
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据准备流水线清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "etl", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条准备任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"准备任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条准备任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="准备任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条准备任务执行启动、完成、失败、重试、暂停、归档；被裁决拦下时说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
