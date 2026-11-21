import { request } from './client'

/** 受控联邦数据分发批次的领域接口封装。 */

export type LicenseHistoryItem = {
  version: number
  action: string
  sources: string[]
  at: string
}

export type Subtotal = {
  category: string
  row_count: number
  subtotal: number
}

export type DeliveryRecord = {
  delivery_id: string
  claimed_at: string
  license_version: string
  granted_sources: string[]
  revoked_sources: string[]
  columns: string[]
  rows: Record<string, unknown>[]
  subtotals: Subtotal[]
  row_count: number
  checksum: string
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail ?? '请求失败'
    throw new Error(Array.isArray(detail) ? JSON.stringify(detail) : String(detail))
  }
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

/** 创建分发申请：申请人能看到查询摘要，接收方与获准来源在此绑定。 */
export function createDispatchApplication(input: {
  receiver: string
  cache_id: number
  requested_sources: string[]
  visible_columns?: string[]
}) {
  return postJson<{
    ok: boolean
    query_summary: string
    cache_sources: string[]
    application: Record<string, unknown>
  }>('/api/fed_dispatch/applications', input)
}

/** 许可变更（整体覆盖获准来源），产生新许可版本。 */
export function updateDispatchLicense(batchId: number, grantedSources: string[]) {
  return postJson<{ ok: boolean; batch_id: number }>(
    `/api/fed_dispatch/${batchId}/license`,
    { granted_sources: grantedSources },
  )
}

/** 撤回单个来源授权。 */
export function revokeDispatchSource(batchId: number, source: string) {
  return postJson<{ ok: boolean }>(`/api/fed_dispatch/${batchId}/revoke`, { source })
}

/** 交付预检：核对当前授权下可交付的行与小计，识别覆盖冲突。 */
export function precheckDispatch(batchId: number) {
  return postJson<{
    ok: boolean
    license_version: string
    requested_sources: string[]
    granted_sources: string[]
    revoked_sources: string[]
    deliverable_rows: number
    coverage_conflict: boolean
    subtotals: Subtotal[]
    message: string
  }>(`/api/fed_dispatch/${batchId}/precheck`)
}

/** 发布批次。 */
export function publishDispatch(batchId: number) {
  return postJson<{ ok: boolean; published_at: string }>(
    `/api/fed_dispatch/${batchId}/publish`,
  )
}

/** 接收方领取：按领取时刻授权交付只读明细与小计。 */
export function claimDispatch(batchId: number) {
  return postJson<{ ok: boolean; coverage_conflict: boolean; delivery: DeliveryRecord }>(
    `/api/fed_dispatch/${batchId}/claim`,
  )
}

/** 许可变更历史，按发布批次核对。 */
export function getDispatchLicenseHistory(batchId: number) {
  return getJson<{ ok: boolean; current_version: string; history: LicenseHistoryItem[] }>(
    `/api/fed_dispatch/${batchId}/license-history`,
  )
}

/** 历史交付：保留每次领取的授权版本与实际数据列。 */
export function getDispatchDeliveries(batchId: number) {
  return getJson<{ ok: boolean; deliveries: DeliveryRecord[] }>(
    `/api/fed_dispatch/${batchId}/deliveries`,
  )
}
