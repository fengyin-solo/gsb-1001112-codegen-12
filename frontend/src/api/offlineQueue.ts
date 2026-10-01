/**
 * 断网本地队列：离线（或请求未送达）时把写操作落 localStorage 排队，
 * 复位后整单 POST /api/snow/sync，服务端按「操作号」幂等重放，
 * 已执行过的操作直接回放结果，不会重复扣料或重复流转。
 */
import { request } from '@/api/client'

export interface QueuedOp {
  操作号: string
  /** 规范化路径，服务端 sync 据此重新分发，例如 /api/snow/events/1/dispatches */
  path: string
  payload: Record<string, any>
  queuedAt: string
  label: string
}

const STORAGE_KEY = 'snow-command-offline-queue-v1'
let seq = 0

export function nextOpNo(): string {
  seq += 1
  const rand = Math.random().toString(36).slice(2, 6)
  return `OP-${Date.now()}-${seq}-${rand}`
}

function load(): QueuedOp[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]') as QueuedOp[]
  } catch {
    return []
  }
}

function persist(ops: QueuedOp[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(ops))
}

export function queueOps(): QueuedOp[] {
  return load()
}

export function enqueue(op: QueuedOp): void {
  const ops = load()
  if (!ops.some((item) => item.操作号 === op.操作号)) {
    ops.push(op)
    persist(ops)
  }
}

export function removeOps(opNos: string[]): void {
  const done = new Set(opNos)
  persist(load().filter((op) => !done.has(op.操作号)))
}

/**
 * 统一写入口：在线直接送服务端（也带操作号）；网络不通时排队等续传。
 * 返回 queued=true 表示只是落了本地队列，调用方按“已受理，复位续传”提示。
 */
export async function snowMutate(
  path: string,
  values: Record<string, any>,
  label: string,
): Promise<Record<string, any>> {
  const opNo = values['操作号'] ? String(values['操作号']) : nextOpNo()
  const payload = { ...values, 操作号: opNo }
  try {
    const response = await request(path, { method: 'POST', body: JSON.stringify({ values: payload }) })
    if (!response.ok) {
      const detail = await response.json().catch(() => null)
      throw new Error(detail?.detail ? String(detail.detail) : `接口返回 ${response.status}`)
    }
    return (await response.json()) as Record<string, any>
  } catch (error) {
    const message = error instanceof Error ? error.message : '请求未送达'
    // 明确断网，或请求压根没送达（服务重启瞬间等），都先排队，保证操作不丢
    if (!navigator.onLine || message.includes('接口请求失败') || message.includes('Failed to fetch')) {
      enqueue({ 操作号: opNo, path, payload, queuedAt: new Date().toLocaleString(), label })
      return {
        ok: true,
        queued: true,
        message: `当前离线，「${label}」已进本地队列（操作号 ${opNo}），联网后自动按序续传`,
      }
    }
    throw error
  }
}

export interface FlushResult {
  flushed: QueuedOp[]
  remaining: QueuedOp[]
  message: string
  results: Array<{ ok: boolean; message: string; 操作号?: string }>
}

/** 复位续传：逐笔回放，成功的出队，业务校验未过的留在队列人工处理。 */
export async function flushQueue(): Promise<FlushResult> {
  const ops = load()
  if (!ops.length) {
    return { flushed: [], remaining: [], message: '本地队列是空的', results: [] }
  }
  const response = await request('/api/snow/sync', {
    method: 'POST',
    body: JSON.stringify({ operations: ops }),
  })
  if (!response.ok) {
    throw new Error(`续传接口返回 ${response.status}，队列原样保留`)
  }
  const data = (await response.json()) as {
    ok: boolean
    message: string
    results: Array<{ ok: boolean; message: string; 操作号?: string }>
  }
  const successNos = data.results.filter((item) => item.ok).map((item) => item.操作号 || '')
  removeOps(successNos)
  return {
    flushed: ops.filter((op) => successNos.includes(op.操作号)),
    remaining: load(),
    message: data.message,
    results: data.results,
  }
}
