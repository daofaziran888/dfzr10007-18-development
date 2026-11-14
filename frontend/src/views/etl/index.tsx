import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/etl'

type Row = Record<string, string | number | null>
type PlanItem = Row & { 可执行动作?: string[] }

const planColumns = ['任务编号', '任务名称', '输入源', '转换步骤', '输出目标', '依赖关系', '运行状态', '断点', '完成水位', '阻塞原因']
const watermarkColumns = ['输出目标', '增量字段', '水位值', '来源任务', '完成时间']
const statuses = ['待处理', '运行中', '已暂停', '已完成', '失败', '已归档']
const createFields = ['任务编号', '任务名称', '输入源', '转换步骤', '输出目标', '依赖关系', '增量字段']

const emptyCreateForm: Record<string, string> = {
  任务编号: '',
  任务名称: '',
  输入源: '',
  转换步骤: '',
  输出目标: '',
  依赖关系: '无',
  增量字段: 'updated_at',
}

export default function EtlView() {
  const [planItems, setPlanItems] = useState<PlanItem[]>([])
  const [watermarks, setWatermarks] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [keyword, setKeyword] = useState('')
  const [status, setStatus] = useState('')
  const [message, setMessage] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState<Record<string, string>>(emptyCreateForm)

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const query = new URLSearchParams()
      if (keyword) query.set('keyword', keyword)
      if (status) query.set('status', status)
      const [listResp, planResp, watermarkResp] = await Promise.all([
        request(`${ENDPOINT}?${query.toString()}`),
        request(`${ENDPOINT}/plan`),
        request(`${ENDPOINT}/watermarks`),
      ])
      if (!listResp.ok || !planResp.ok || !watermarkResp.ok) {
        throw new Error('流水线计划读取失败')
      }
      const listPayload = await listResp.json()
      const planPayload = await planResp.json()
      const watermarkPayload = await watermarkResp.json()
      setTotal(listPayload.total ?? 0)
      setPlanItems(planPayload.items ?? [])
      setWatermarks(watermarkPayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据准备流水线列表读取失败')
    }
  }, [keyword, status])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => {
    const count = (name: string) => planItems.filter((item) => item.运行状态 === name).length
    return [
      { label: '准备任务总数', value: planItems.length },
      { label: '运行中任务', value: count('运行中') },
      { label: '待处理任务', value: count('待处理') },
      { label: '失败任务', value: count('失败') },
    ]
  }, [planItems])

  async function runAction(action: string, row: Row) {
    setMessage('')
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id ?? ''}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '数据准备流水线动作未生效')
      }
      setMessage(payload.message ?? '动作已执行')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据准备流水线操作失败')
      await reload()
    }
  }

  async function runPlanAction(action: string, item: PlanItem) {
    await runAction(action, item)
  }

  async function createEntry() {
    setMessage('')
    setErrorMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: createForm }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? payload.detail ?? '准备任务登记失败')
      }
      setMessage(payload.message ?? '准备任务已登记')
      setCreateForm(emptyCreateForm)
      setShowCreate(false)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '准备任务登记失败')
    }
  }

  return (
    <section className="page" data-module="etl">
      <header className="page-head">
        <div>
          <h2>数据准备流水线管理</h2>
          <p className="page-desc">按输入源、转换步骤、依赖关系与输出目标生成逐任务运行状态，完成水位交给数据定时刷新读取。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate((prev) => !prev)}>
            登记准备任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据准备流水线清单
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

      {showCreate && (
        <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void createEntry() }}>
          {createFields.map((field) => (
            <label key={field} className="filter-item">
              <span>{field}</span>
              <input
                value={createForm[field] ?? ''}
                placeholder={`请输入${field}`}
                onChange={(event) => setCreateForm((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            </label>
          ))}
          <button className="btn primary" type="submit">提交登记</button>
        </form>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>任务编号</span>
          <input value={keyword} placeholder="按任务编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>运行状态</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部状态</option>
            {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setKeyword(''); setStatus('') }}>重置条件</button>
      </form>

      <h3 className="section-title">流水线计划（逐任务运行状态）</h3>
      <table className="data-table">
        <thead>
          <tr>
            {planColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {planItems.map((item) => (
            <tr key={String(item.任务编号)}>
              {planColumns.map((column) => (
                <td key={column} className={column === '阻塞原因' && item[column] ? 'error-text' : undefined}>
                  {item[column] || '—'}
                </td>
              ))}
              <td className="row-actions">
                {(item.可执行动作 ?? []).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runPlanAction(action, item)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {planItems.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={planColumns.length + 1}>暂无数据：当前没有可运行的准备任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">完成水位（供数据定时刷新读取）</h3>
      <table className="data-table">
        <thead>
          <tr>
            {watermarkColumns.map((column) => <th key={column}>{column}</th>)}
          </tr>
        </thead>
        <tbody>
          {watermarks.map((row) => (
            <tr key={String(row.输出目标)}>
              {watermarkColumns.map((column) => <td key={column}>{row[column] || '—'}</td>)}
            </tr>
          ))}
          {watermarks.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={watermarkColumns.length}>暂无数据：还没有任务产出完成水位</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据准备流水线记录</span>
        {message && <span className="ok-text">{message}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
