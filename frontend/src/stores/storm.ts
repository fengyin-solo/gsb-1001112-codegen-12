/**
 * 风雪指挥板写操作的离线队列。
 *
 * 每条写操作在发起前就分配全局唯一操作号：
 * - 在线时操作号随请求上送，后端据此去重；
 * - 断网（或请求未送达）时操作连同操作号进本地队列并持久化到 localStorage；
 * - 复位后整批调用 /api/storm/sync 续传，后端按操作号幂等回放，
 *   重复提交（含刷新页面、多次点击）不会二次扣减材料或重复占用车辆。
 *
 * 业务校验失败（HTTP 200 且 ok:false，例如库存不足、跳状态）不进队列——
 * 这种操作重试多少次都不会成功，应当场把原因退回给调度员修改。
 */
import { defineStore } from 'pinia'

import { request } from '@/api/client'

export type StormOpType = 'create_event' | 'transition' | 'dispatch' | 'report_position'

export interface QueuedOp {
  操作号: string
  操作类型: StormOpType
  values: Record<string, unknown>
  enqueuedAt: string
}

const STORAGE_KEY = 'storm-offline-queue-v1'

let seq = 0

/** 操作号：时间戳 + 进程内自增序列 + 随机串，断网多端排队也不撞号。 */
export function nextOpNo(): string {
  seq += 1
  const rand = Math.random().toString(36).slice(2, 8)
  const pad = String(seq).padStart(3, '0')
  return `OP-${Date.now()}-${pad}-${rand}`
}

/** 各操作类型的即时提交地址；sync 批量续传时只传操作号/类型/参数。 */
function endpointOf(type: StormOpType, values: Record<string, unknown>): string {
  switch (type) {
    case 'create_event':
      return '/api/storm/events'
    case 'transition':
      return `/api/storm/events/${String(values['事件ID'])}/transition`
    case 'dispatch':
      return '/api/storm/dispatches'
    case 'report_position':
      return '/api/storm/positions'
  }
}

export class BusinessReject extends Error {}

export const useStormQueueStore = defineStore('storm-queue', {
  state: () => ({
    /** 浏览器实际网络状态 */
    online: typeof navigator === 'undefined' ? true : navigator.onLine,
    /** 手动模拟断网的开关，方便在演示环境验证排队与续传 */
    forceOffline: false,
    queue: [] as QueuedOp[],
    syncing: false,
    lastSyncMessage: '',
    loaded: false,
  }),
  getters: {
    effectiveOnline: (state) => state.online && !state.forceOffline,
    pendingCount: (state) => state.queue.length,
  },
  actions: {
    restore() {
      if (this.loaded) return
      this.loaded = true
      try {
        const raw = localStorage.getItem(STORAGE_KEY)
        if (raw) {
          this.queue = JSON.parse(raw) as QueuedOp[]
        }
      } catch {
        this.queue = []
      }
      if (typeof window !== 'undefined') {
        window.addEventListener('online', () => {
          this.online = true
          void this.flush()
        })
        window.addEventListener('offline', () => {
          this.online = false
        })
      }
    },
    persist() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.queue))
    },
    toggleForceOffline() {
      this.forceOffline = !this.forceOffline
      if (!this.forceOffline) {
        void this.flush()
      }
    },

    /**
     * 提交一条写操作。
     * 成功或业务校验失败都返回后端响应体；仅网络不通时把操作排入本地队列，
     * 并返回 { ok: true, queued: true } 让页面提示"已排队，复位后续传"。
     */
    async submit(
      type: StormOpType,
      values: Record<string, unknown>,
    ): Promise<Record<string, any>> {
      this.restore()
      const opNo = nextOpNo()
      if (!this.effectiveOnline) {
        this.enqueue({ 操作号: opNo, 操作类型: type, values, enqueuedAt: new Date().toISOString() })
        return { ok: true, queued: true, message: '当前处于断网状态，操作已本地排队，复位后按操作号幂等续传' }
      }
      try {
        const response = await request(endpointOf(type, values), {
          method: 'POST',
          body: JSON.stringify({ values, op_no: opNo }),
        })
        if (response.status >= 500) {
          this.enqueue({ 操作号: opNo, 操作类型: type, values, enqueuedAt: new Date().toISOString() })
          return { ok: true, queued: true, message: '服务暂不可用，操作已本地排队，复位后续传' }
        }
        return (await response.json()) as Record<string, any>
      } catch {
        // 请求未送达（断网 / 后端未起）：排队等续传。
        this.enqueue({ 操作号: opNo, 操作类型: type, values, enqueuedAt: new Date().toISOString() })
        return { ok: true, queued: true, message: '网络中断，操作已本地排队，复位后按操作号幂等续传' }
      }
    },

    enqueue(op: QueuedOp) {
      this.queue.push(op)
      this.persist()
    },
    removeOp(opNo: string) {
      this.queue = this.queue.filter((item) => item.操作号 !== opNo)
      this.persist()
    },

    /** 复位后整批续传：成功（含幂等命中）的出队，仍失败的保留排队。 */
    async flush(): Promise<{ flushed: boolean; message: string }> {
      this.restore()
      if (this.syncing || !this.queue.length) {
        return { flushed: false, message: this.lastSyncMessage }
      }
      if (!this.effectiveOnline) {
        return { flushed: false, message: '仍处于断网状态，继续排队等待' }
      }
      this.syncing = true
      const batch = this.queue.map((op) => ({
        操作号: op.操作号,
        操作类型: op.操作类型,
        values: op.values,
      }))
      try {
        const response = await request('/api/storm/sync', {
          method: 'POST',
          body: JSON.stringify({ operations: batch }),
        })
        if (!response.ok) {
          throw new Error(`sync ${response.status}`)
        }
        const body = (await response.json()) as {
          synced: number
          replayed: number
          results: { 操作号: string; ok: boolean; message?: string }[]
          message: string
        }
        const failed = body.results.filter((r) => !r.ok)
        for (const item of body.results) {
          if (item.ok) {
            this.removeOp(item.操作号)
          }
        }
        const note = failed.length
          ? `，${failed.length} 条仍被业务规则拒绝并保留排队：${failed[0].message ?? ''}`
          : ''
        this.lastSyncMessage = `${body.message}${note}`
        return { flushed: body.synced + body.replayed > 0, message: this.lastSyncMessage }
      } catch {
        this.lastSyncMessage = '续传未送达，队列保持不变，等待下次复位'
        return { flushed: false, message: this.lastSyncMessage }
      } finally {
        this.syncing = false
      }
    },
  },
})
