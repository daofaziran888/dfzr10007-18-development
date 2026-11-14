import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/refresh'
const columns = [
  "策略编号",
  "目标数据集",
  "刷新周期",
  "刷新范围",
  "增量字段",
  "上游水位",
  "水位来源任务",
  "上次刷新水位",
  "下一次刷新",
  "策略状态"
]
const actions = [
  "启用刷新",
  "暂停刷新",
  "关闭刷新",
  "执行刷新"
]
const statuses = [
  "已创建",
  "生效中",
  "已暂停",
  "已关闭"
]

type Row = Record<string, string | number | null>

export default function RefreshView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('刷新策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据定时刷新列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => {
    const count = (name: string) => rows.filter((row) => row.status === name || row.策略状态 === name).length
    return [
      { label: '刷新策略总数', value: rows.length },
      { label: '生效中策略', value: count('生效中') },
      { label: '已暂停刷新', value: count('已暂停') },
    ]
  }, [rows])

  async function runAction(action: string, row: Row) {
    setMessage('')
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '数据定时刷新动作未生效，请稍后重试')
      }
      setMessage(payload.message ?? '动作已执行')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据定时刷新操作失败')
      await reload()
    }
  }

  return (
    <section className="page" data-module="refresh">
      <header className="page-head">
        <div>
          <h2>数据定时刷新管理</h2>
          <p className="page-desc">维护数据刷新的周期配置、范围控制与增量策略，执行刷新时读取数据准备流水线的完成水位，从对应增量字段继续。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('刷新策略登记入口尚未接入审批流')}>
            登记刷新策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据定时刷新清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column} className={column === '上游水位' && !row[column] ? 'error-text' : undefined}>
                  {column === '上游水位' && !row[column]
                    ? (row.上游异常 ? String(row.上游异常) : '暂无水位')
                    : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据定时刷新数据，可先登记刷新策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据定时刷新记录</span>
        {message && <span className="ok-text">{message}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
