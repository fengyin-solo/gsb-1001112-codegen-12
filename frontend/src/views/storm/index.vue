<template>
  <section class="page storm-board" data-module="storm">
    <header class="page-head">
      <div>
        <h2>风雪事件指挥板</h2>
        <p class="page-desc">
          把气象时段、作业路段与车辆位置编排成连续事件窗口；封控段可直接调用材料与班组，
          调度结论同引一个事件写入除雪台账、路段阶段与物资清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="toggleOffline">
          {{ queue.effectiveOnline ? '模拟断网' : '恢复联网' }}
        </button>
        <button class="btn" type="button" :disabled="queue.syncing" @click="flushQueue">
          续传排队<span v-if="queue.pendingCount" class="badge">{{ queue.pendingCount }}</span>
        </button>
        <button class="btn primary" type="button" @click="openCreate">登记风雪事件</button>
      </div>
    </header>

    <div v-if="!queue.effectiveOnline" class="offline-banner">
      当前断网：写操作进入本地排队（{{ queue.pendingCount }} 条），网络复位后按操作号幂等续传，不重复扣减。
    </div>
    <div v-else-if="queue.pendingCount" class="wait-banner">
      网络已恢复，尚有 {{ queue.pendingCount }} 条排队操作待续传。
      <button class="link" type="button" @click="flushQueue">立即续传</button>
    </div>
    <p v-if="syncMessage" class="sync-note">{{ syncMessage }}</p>

    <div class="stat-row">
      <article v-for="item in cards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>事件状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statusOrder" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>关键字</span>
        <input v-model="keyword" placeholder="按事件编号或名称检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilter">重置条件</button>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>事件编号</th>
          <th>事件名称</th>
          <th>气象时段（连续窗口范围）</th>
          <th>天气基准</th>
          <th>应急指挥令</th>
          <th>状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="ev in events" :key="String(ev.id)">
          <td>{{ ev['事件编号'] }}</td>
          <td>{{ ev['事件名称'] }}</td>
          <td>{{ fmt(ev['气象开始']) }} ～ {{ fmt(ev['气象结束']) }}</td>
          <td class="baseline-cell">{{ ev['天气基准'] }}</td>
          <td>{{ ev['应急指挥令'] || '—' }}</td>
          <td><span class="status-tag" :data-status="ev.status">{{ ev.status }}</span></td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(ev)">指挥板详情</button>
            <button
              v-if="ev.status !== '已关闭'"
              class="link"
              type="button"
              :data-act="nextAction(ev.status)"
              @click="advance(ev)"
            >
              {{ nextAction(ev.status) }}
            </button>
            <span v-else class="muted-text">已归档冻结</span>
          </td>
        </tr>
        <tr v-if="!events.length">
          <td colspan="7" class="empty-state">暂无风雪事件，可先登记一次风雪过程并编排连续窗口</td>
        </tr>
      </tbody>
    </table>

    <!-- 排队操作 -->
    <section v-if="queue.queue.length" class="queue-panel">
      <h3>断网排队操作（本地持久化）</h3>
      <table class="data-table">
        <thead>
          <tr><th>操作号</th><th>操作类型</th><th>排队时间</th><th>摘要</th></tr>
        </thead>
        <tbody>
          <tr v-for="op in queue.queue" :key="op.操作号">
            <td class="op-no">{{ op.操作号 }}</td>
            <td>{{ opTypeLabel(op.操作类型) }}</td>
            <td>{{ fmt(op.enqueuedAt) }}</td>
            <td>{{ opSummary(op) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- ================= 事件详情（指挥板） ================= -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal modal-wide">
        <header class="modal-head">
          <div>
            <h3>{{ detail.event['事件编号'] }} · {{ detail.event['事件名称'] }}</h3>
            <p class="page-desc">
              天气基准：{{ detail.event['天气基准'] }}
              <span v-if="detail.event['应急指挥令']">｜指挥令 {{ detail.event['应急指挥令'] }}</span>
            </p>
          </div>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </header>

        <!-- 连续事件窗口：按路段分组、时段首尾相接 -->
        <section class="detail-section">
          <h4>连续事件窗口（气象时段 × 作业路段 × 车辆位置）</h4>
          <div v-for="group in groupedWindows" :key="group.road" class="road-group">
            <div class="road-group-head">
              <strong>{{ group.road }}</strong>
              <span class="status-tag" :data-status="group.segmentStatus">{{ group.segmentStatus }}</span>
              <span v-if="floodConflict(group.road)" class="conflict-tag">
                防汛 {{ floodConflict(group.road) }} 在办，须凭指挥令定优先级
              </span>
            </div>
            <div class="window-strip">
              <div v-for="w in group.windows" :key="String(w.id)" class="window-card" :class="{ closed: w['窗口状态'] === '已完结' }">
                <div class="window-time">{{ fmt(w['开始时间']) }} → {{ fmt(w['结束时间']) }}</div>
                <div class="window-name">{{ w['时段名称'] }}｜{{ w['时段天气'] || '天气随事件基准' }}</div>
                <div class="window-meta">班组：{{ w['班组'] || '未派' }}｜车辆：{{ plateText(w['作业车辆']) || '未派' }}</div>
                <div class="window-meta">窗口：{{ w['窗口状态'] }}</div>
                <button
                  v-if="canDispatch"
                  class="btn small"
                  type="button"
                  :disabled="w['路段状态'] !== '封控'"
                  :title="w['路段状态'] !== '封控' ? '只有封控段可直接调用材料与班组' : ''"
                  @click="openDispatch(w)"
                >
                  {{ w['路段状态'] === '封控' ? '调用材料与班组' : `非封控段（${w['路段状态']}）不可直调` }}
                </button>
              </div>
            </div>
          </div>
        </section>

        <!-- 车辆位置时间线 -->
        <section class="detail-section">
          <h4>
            车辆位置
            <button v-if="detail.event.status !== '已关闭'" class="btn small" type="button" @click="openPosition">上报位置</button>
          </h4>
          <table class="data-table" v-if="detail.positions.length">
            <thead><tr><th>上报时间</th><th>车辆</th><th>位置（桩号）</th></tr></thead>
            <tbody>
              <tr v-for="p in detail.positions" :key="String(p.id)">
                <td>{{ fmt(p['上报时间']) }}</td>
                <td>{{ p['车牌号'] || `车辆${p['车辆ID']}` }}</td>
                <td>{{ p['位置'] }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted-text">尚无位置上报。</p>
        </section>

        <!-- 调度结论：三处同引事件 -->
        <section class="detail-section">
          <h4>调度会话结论（三处引用同一事件 {{ detail.event['事件编号'] }}）</h4>

          <p class="ledger-title">① 除雪作业台账</p>
          <table class="data-table" v-if="detail.winter_ledger.length">
            <thead><tr><th>作业编号</th><th>派发编号</th><th>路段</th><th>日期</th><th>班组</th><th>融雪剂用量</th><th>路面状况（当时基准）</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in detail.winter_ledger" :key="String(row.id)">
                <td>{{ row['作业编号'] }}</td>
                <td>{{ row['派发编号'] }}</td>
                <td>{{ row['作业路段'] }}</td>
                <td>{{ row['作业日期'] }}</td>
                <td>{{ row['作业班组'] }}</td>
                <td>{{ row['融雪剂用量'] }}</td>
                <td>{{ row['路面状况'] }}</td>
                <td>{{ row['作业状态'] }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted-text">尚未形成调度结论。</p>

          <p class="ledger-title">② 路段阶段</p>
          <table class="data-table" v-if="detail.road_phases.length">
            <thead><tr><th>派发编号</th><th>路段</th><th>阶段</th><th>阶段窗口</th><th>管控</th><th>班组</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in detail.road_phases" :key="String(row.id)">
                <td>{{ row['派发编号'] }}</td>
                <td>{{ row['作业路段'] }}</td>
                <td>{{ row['阶段'] }}</td>
                <td>{{ fmt(row['开始时间']) }}～{{ fmt(row['结束时间']) }}</td>
                <td>{{ row['管控状态'] }}</td>
                <td>{{ row['班组'] }}</td>
                <td>{{ row.status }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted-text">尚未形成调度结论。</p>

          <p class="ledger-title">③ 物资清单（材料扣减）</p>
          <table class="data-table" v-if="detail.material_items.length">
            <thead><tr><th>派发编号</th><th>材料</th><th>扣减数量</th><th>路段</th><th>用途</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in detail.material_items" :key="String(row.id)">
                <td>{{ row['派发编号'] }}</td>
                <td>{{ row['材料名称'] }}</td>
                <td>{{ row['扣减数量'] }}{{ row['计量单位'] }}</td>
                <td>{{ row['作业路段'] }}</td>
                <td>{{ row['用途说明'] }}</td>
                <td>{{ row.status }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="muted-text">尚未形成调度结论。</p>

          <p class="ledger-title">车辆占用（事务相符）</p>
          <table class="data-table" v-if="detail.vehicle_occ.length">
            <thead><tr><th>派发编号</th><th>车辆</th><th>路段</th><th>占用开始</th><th>占用结束</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in detail.vehicle_occ" :key="String(row.id)">
                <td>{{ row['派发编号'] }}</td>
                <td>{{ plateText(String(row['车辆ID'])) }}</td>
                <td>{{ row['作业路段'] }}</td>
                <td>{{ fmt(row['占用开始']) }}</td>
                <td>{{ row['占用结束'] ? fmt(row['占用结束']) : '—' }}</td>
                <td>{{ row.status }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>

    <!-- ================= 新建事件 ================= -->
    <div v-if="createForm.show" class="modal-mask" @click.self="createForm.show = false">
      <div class="modal">
        <header class="modal-head">
          <h3>登记风雪事件并编排连续窗口</h3>
          <button class="btn ghost" type="button" @click="createForm.show = false">取消</button>
        </header>
        <div class="form-grid">
          <label><span>事件名称 *</span><input v-model="createForm['事件名称']" placeholder="如：元旦强降雪过程" /></label>
          <label><span>天气基准 *</span><input v-model="createForm['天气基准']" placeholder="预警级别、天气、气温、风力，关闭后冻结" /></label>
          <label><span>气象开始 *</span><input v-model="createForm['气象开始']" type="datetime-local" /></label>
          <label><span>气象结束 *</span><input v-model="createForm['气象结束']" type="datetime-local" /></label>
          <label class="span2"><span>应急指挥令（有防汛冲突时必填）</span><input v-model="createForm['应急指挥令']" placeholder="如：YJ-2026-1001-09" /></label>
        </div>

        <div class="sub-head">
          <h4>气象时段（必须首尾相接铺满整个气象时段，不能有间隙或重叠）</h4>
          <button class="btn small" type="button" @click="fillSinglePhase">用一个时段铺满</button>
        </div>
        <div v-for="(p, i) in createForm.phases" :key="i" class="dynamic-row">
          <input v-model="p.name" placeholder="时段名称，如 雪前预撒" />
          <input v-model="p.weather" placeholder="时段天气" />
          <input v-model="p.start" type="datetime-local" title="开始" />
          <input v-model="p.end" type="datetime-local" title="结束" />
          <button class="btn ghost small" type="button" @click="createForm.phases.splice(i, 1)">删除</button>
        </div>
        <button class="btn small" type="button" @click="addPhase">增加气象时段</button>

        <div class="sub-head"><h4>作业路段（封控段可在执行中直接调用材料与班组）</h4></div>
        <div v-for="(s, i) in createForm.segments" :key="i" class="dynamic-row">
          <input v-model="s.road" placeholder="作业路段，如 北二环高架 K3+200~K6+800" />
          <select v-model="s.segment_status">
            <option v-for="st in segmentStatuses" :key="st" :value="st">{{ st }}</option>
          </select>
          <button class="btn ghost small" type="button" @click="createForm.segments.splice(i, 1)">删除</button>
        </div>
        <button class="btn small" type="button" @click="addSegment">增加作业路段</button>

        <p v-if="createForm.error" class="error-text">{{ createForm.error }}</p>
        <footer class="modal-foot">
          <button class="btn primary" type="button" @click="submitCreate">登记并生成窗口</button>
        </footer>
      </div>
    </div>

    <!-- ================= 封控段调度：调用材料与班组 ================= -->
    <div v-if="dispatchForm.show" class="modal-mask" @click.self="dispatchForm.show = false">
      <div class="modal">
        <header class="modal-head">
          <h3>封控段调度会话 · 调用材料与班组</h3>
          <button class="btn ghost" type="button" @click="dispatchForm.show = false">取消</button>
        </header>
        <p class="page-desc">
          窗口：{{ dispatchWindow?.['时段名称'] }} {{ dispatchWindow && fmt(dispatchWindow['开始时间']) }}～{{ dispatchWindow && fmt(dispatchWindow['结束时间']) }}
          ｜路段：{{ dispatchWindow?.['作业路段'] }}（封控）
        </p>
        <div v-if="dispatchConflict" class="conflict-box">
          防汛记录 {{ dispatchConflict }} 正在占用该路段。除雪与防汛同时占用道路，
          须填写应急指挥令确定优先级后方可派发。
        </div>
        <div class="form-grid">
          <label><span>责任班组 *</span><input v-model="dispatchForm['班组']" placeholder="如：甲班除雪一组" /></label>
          <label><span>应急指挥令</span><input v-model="dispatchForm['应急指挥令']" :placeholder="dispatchConflict ? '冲突路段必填，如 YJ-2026-1001-09' : '无冲突可不填'" /></label>
          <label class="span2"><span>调度会话结论 / 派发说明 *</span><input v-model="dispatchForm['派发说明']" placeholder="结论将同时写入台账、路段阶段与物资清单" /></label>
        </div>

        <div class="sub-head"><h4>占用车辆（事务一致：在库车辆方可派发）</h4></div>
        <div class="pick-grid">
          <label v-for="v in vehicles" :key="String(v.id)" class="pick-item" :class="{ disabled: v.status !== '在库' }">
            <input type="checkbox" :value="Number(v.id)" v-model="dispatchForm.vehicleIds" :disabled="v.status !== '在库'" />
            {{ v['车牌号'] }}（{{ v['车辆类型'] }}，{{ v.status }}）
          </label>
        </div>

        <div class="sub-head"><h4>调用材料（即时扣减库存，库存不足整单回滚）</h4></div>
        <div v-for="(line, i) in dispatchForm.materials" :key="i" class="dynamic-row">
          <select v-model="line.materialId">
            <option :value="0" disabled>选择材料</option>
            <option v-for="m in materials" :key="String(m.id)" :value="Number(m.id)">
              {{ m['材料名称'] }}（库存 {{ m['库存数量'] }}{{ m['计量单位'] }}）
            </option>
          </select>
          <input v-model.number="line.qty" type="number" min="0" step="0.1" placeholder="扣减数量" />
          <button class="btn ghost small" type="button" @click="dispatchForm.materials.splice(i, 1)">删除</button>
        </div>
        <button class="btn small" type="button" @click="dispatchForm.materials.push({ materialId: 0, qty: 1 })">增加材料</button>

        <p v-if="dispatchForm.error" class="error-text">{{ dispatchForm.error }}</p>
        <footer class="modal-foot">
          <button class="btn primary" type="button" @click="submitDispatch">形成调度结论并事务提交</button>
        </footer>
      </div>
    </div>

    <!-- ================= 车辆位置上报 ================= -->
    <div v-if="positionForm.show" class="modal-mask" @click.self="positionForm.show = false">
      <div class="modal modal-narrow">
        <header class="modal-head">
          <h3>上报车辆位置</h3>
          <button class="btn ghost" type="button" @click="positionForm.show = false">取消</button>
        </header>
        <div class="form-grid">
          <label class="span2">
            <span>车辆 *</span>
            <select v-model="positionForm.vehicleId">
              <option :value="0" disabled>选择车辆</option>
              <option v-for="v in vehicles" :key="String(v.id)" :value="Number(v.id)">{{ v['车牌号'] }}（{{ v['车辆类型'] }}）</option>
            </select>
          </label>
          <label class="span2"><span>位置 / 桩号 *</span><input v-model="positionForm['位置']" placeholder="如 K4+550 北幅主路" /></label>
        </div>
        <p v-if="positionForm.error" class="error-text">{{ positionForm.error }}</p>
        <footer class="modal-foot">
          <button class="btn primary" type="button" @click="submitPosition">挂接到事件窗口</button>
        </footer>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'
import { useStormQueueStore, type QueuedOp } from '@/stores/storm'

type AnyRow = Record<string, any>

const queue = useStormQueueStore()

const statusOrder = ['待启动', '执行中', '收尾', '已关闭']
const segmentStatuses = ['封控', '限速', '通行']

const events = ref<AnyRow[]>([])
const windows = ref<AnyRow[]>([])
const vehicles = ref<AnyRow[]>([])
const materials = ref<AnyRow[]>([])
const floodActive = ref<AnyRow[]>([])
const cards = ref<{ label: string; value: number }[]>([])

const statusFilter = ref('')
const keyword = ref('')
const errorMessage = ref('')
const syncMessage = ref('')

const detail = ref<{
  event: AnyRow
  windows: AnyRow[]
  dispatches: AnyRow[]
  winter_ledger: AnyRow[]
  road_phases: AnyRow[]
  material_items: AnyRow[]
  vehicle_occ: AnyRow[]
  positions: AnyRow[]
} | null>(null)

function nextAction(status: string): string {
  const map: Record<string, string> = { 待启动: '启动事件', 执行中: '转入收尾', 收尾: '关闭事件' }
  return map[status] ?? ''
}

function fmt(value: string): string {
  if (!value) return '—'
  return value.replace('T', ' ')
}

function plateText(ids: string): string {
  if (!ids) return ''
  return ids
    .split(',')
    .filter(Boolean)
    .map((id) => vehicles.value.find((v) => String(v.id) === id)?.['车牌号'] || `车辆${id}`)
    .join('、')
}

function floodConflict(road: string): string {
  const hit = floodActive.value.find((f) => f['影响路段'] === road)
  return hit ? String(hit['记录编号']) : ''
}

const groupedWindows = computed(() => {
  if (!detail.value) return []
  const map = new Map<string, AnyRow[]>()
  for (const w of detail.value.windows) {
    const list = map.get(w['作业路段']) ?? []
    list.push(w)
    map.set(w['作业路段'], list)
  }
  return [...map.entries()].map(([road, list]) => ({
    road,
    segmentStatus: list[0]['路段状态'],
    windows: [...list].sort((a, b) => String(a['开始时间']).localeCompare(String(b['开始时间']))),
  }))
})

const canDispatch = computed(() => detail.value?.event?.status === '执行中')

// ---------------------------------------------------------------- 数据读取
async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (statusFilter.value) query.set('status', statusFilter.value)
  if (keyword.value) query.set('keyword', keyword.value)
  try {
    const response = await request(`/api/storm/board?${query.toString()}`)
    if (!response.ok) throw new Error('指挥板数据读取失败')
    const body = await response.json()
    events.value = body.events ?? []
    windows.value = body.windows ?? []
    vehicles.value = body.vehicles ?? []
    materials.value = body.materials ?? []
    floodActive.value = body.flood_active ?? []
    cards.value = body.cards ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指挥板数据读取失败'
  }
}

function resetFilter() {
  statusFilter.value = ''
  keyword.value = ''
  void reload()
}

async function openDetail(ev: AnyRow) {
  try {
    const response = await request(`/api/storm/events/${Number(ev.id)}`)
    if (!response.ok) throw new Error('事件详情读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件详情读取失败'
  }
}

// ---------------------------------------------------------------- 状态流转
async function advance(ev: AnyRow) {
  errorMessage.value = ''
  const action = nextAction(String(ev.status))
  const result = await queue.submit('transition', { 事件ID: Number(ev.id), action })
  await afterWrite(result, action === '关闭事件' ? '事件已关闭，台账与天气基准冻结' : `已${action}`)
}

async function afterWrite(result: Record<string, any>, okHint: string) {
  syncMessage.value = ''
  if (result.queued) {
    syncMessage.value = String(result.message)
    return
  }
  if (!result.ok) {
    errorMessage.value = String(result.message || '操作未生效')
    return
  }
  syncMessage.value = String(result.message || okHint)
  await reload()
  if (detail.value) {
    await openDetail(detail.value.event)
  }
}

// ---------------------------------------------------------------- 排队 / 断网
function toggleOffline() {
  queue.toggleForceOffline()
  syncMessage.value = queue.effectiveOnline ? '已恢复联网' : '已切换为模拟断网，写操作将本地排队'
}

async function flushQueue() {
  const r = await queue.flush()
  syncMessage.value = r.message
  if (r.flushed) {
    await reload()
    if (detail.value) await openDetail(detail.value.event)
  }
}

function opTypeLabel(type: string): string {
  return { create_event: '登记事件', transition: '状态流转', dispatch: '调度派发', report_position: '位置上报' }[type] ?? type
}

function opSummary(op: QueuedOp): string {
  const v = op.values
  if (op.操作类型 === 'transition') return `事件#${v['事件ID']} ${v.action}`
  if (op.操作类型 === 'dispatch') return `${v['作业路段'] ?? ''} ${v['班组'] ?? ''}`
  if (op.操作类型 === 'report_position') return `车辆#${v['车辆ID']} @ ${v['位置']}`
  return String(v['事件名称'] ?? '')
}

// ---------------------------------------------------------------- 新建事件
interface PhaseRow { name: string; weather: string; start: string; end: string }
interface SegmentRow { road: string; segment_status: string }

const emptyCreate = () => ({
  show: false,
  '事件名称': '',
  '天气基准': '',
  '气象开始': '',
  '气象结束': '',
  '应急指挥令': '',
  phases: [] as PhaseRow[],
  segments: [{ road: '', segment_status: '封控' }] as SegmentRow[],
  error: '',
})
const createForm = reactive(emptyCreate())

function openCreate() {
  Object.assign(createForm, emptyCreate())
  createForm.show = true
}
function addPhase() {
  createForm.phases.push({ name: '', weather: '', start: '', end: '' })
}
function addSegment() {
  createForm.segments.push({ road: '', segment_status: '封控' })
}
function fillSinglePhase() {
  createForm.phases = [{
    name: '全程作业',
    weather: createForm['天气基准'],
    start: createForm['气象开始'],
    end: createForm['气象结束'],
  }]
}

async function submitCreate() {
  createForm.error = ''
  const values = {
    事件名称: createForm['事件名称'],
    天气基准: createForm['天气基准'],
    气象开始: createForm['气象开始'],
    气象结束: createForm['气象结束'],
    应急指挥令: createForm['应急指挥令'],
    气象时段: createForm.phases.map((p) => ({ name: p.name, weather: p.weather, start: p.start, end: p.end })),
    作业路段: createForm.segments.map((s) => ({ road: s.road, segment_status: s.segment_status })),
  }
  const result = await queue.submit('create_event', values)
  if (result.queued) {
    createForm.show = false
    syncMessage.value = String(result.message)
    return
  }
  if (!result.ok) {
    createForm.error = String(result.message)
    return
  }
  createForm.show = false
  await reload()
  if (result.event) await openDetail(result.event)
}

// ---------------------------------------------------------------- 调度派发
interface MaterialLine { materialId: number; qty: number }

const emptyDispatch = () => ({
  show: false,
  '班组': '',
  '派发说明': '',
  '应急指挥令': '',
  vehicleIds: [] as number[],
  materials: [{ materialId: 0, qty: 1 }] as MaterialLine[],
  error: '',
})
const dispatchForm = reactive(emptyDispatch())
const dispatchEventId = ref(0)
const dispatchWindow = ref<AnyRow | null>(null)
const dispatchConflict = ref('')

function openDispatch(w: AnyRow) {
  if (!detail.value) return
  Object.assign(dispatchForm, emptyDispatch())
  dispatchEventId.value = Number(detail.value.event.id)
  dispatchWindow.value = w
  dispatchConflict.value = floodConflict(String(w['作业路段']))
  dispatchForm.show = true
}

async function submitDispatch() {
  dispatchForm.error = ''
  if (!dispatchWindow.value) return
  const values = {
    事件ID: dispatchEventId.value,
    窗口ID: Number(dispatchWindow.value.id),
    班组: dispatchForm['班组'],
    派发说明: dispatchForm['派发说明'],
    应急指挥令: dispatchForm['应急指挥令'],
    车辆IDs: dispatchForm.vehicleIds,
    材料: dispatchForm.materials
      .filter((line) => line.materialId > 0 && Number(line.qty) > 0)
      .map((line) => ({ 材料ID: line.materialId, 数量: Number(line.qty) })),
  }
  const result = await queue.submit('dispatch', values)
  if (result.queued) {
    dispatchForm.show = false
    syncMessage.value = String(result.message)
    return
  }
  if (!result.ok) {
    dispatchForm.error = String(result.message)
    return
  }
  dispatchForm.show = false
  await reload()
  if (detail.value) await openDetail(detail.value.event)
  syncMessage.value = String(result.message)
}

// ---------------------------------------------------------------- 位置上报
const emptyPosition = () => ({ show: false, vehicleId: 0, '位置': '', error: '' })
const positionForm = reactive(emptyPosition())

function openPosition() {
  Object.assign(positionForm, emptyPosition())
  positionForm.show = true
}

async function submitPosition() {
  positionForm.error = ''
  if (!detail.value) return
  const result = await queue.submit('report_position', {
    事件ID: Number(detail.value.event.id),
    车辆ID: positionForm.vehicleId,
    位置: positionForm['位置'],
  })
  if (result.queued) {
    positionForm.show = false
    syncMessage.value = String(result.message)
    return
  }
  if (!result.ok) {
    positionForm.error = String(result.message)
    return
  }
  positionForm.show = false
  await openDetail(detail.value.event)
  syncMessage.value = String(result.message)
}

onMounted(() => {
  queue.restore()
  void reload()
})
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; align-items: center; }
.badge {
  display: inline-block; margin-left: 4px; min-width: 18px; padding: 0 5px;
  background: #b42318; color: #fff; border-radius: 9px; font-size: 12px; text-align: center;
}
.offline-banner, .wait-banner {
  margin-bottom: 12px; padding: 8px 12px; border-radius: 6px; font-size: 13px;
}
.offline-banner { background: #fef3c7; border: 1px solid #f59e0b; color: #92400e; }
.wait-banner { background: #dbeafe; border: 1px solid #60a5fa; color: #1e40af; }
.sync-note { color: #1f6feb; font-size: 12px; margin: 4px 0 8px; }
.baseline-cell { max-width: 260px; color: #475569; }
.muted-text { color: var(--muted); font-size: 12px; }
.status-tag {
  display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px;
  background: #e2e8f0; color: #334155;
}
.status-tag[data-status='待启动'] { background: #fef9c3; color: #854d0e; }
.status-tag[data-status='执行中'] { background: #dbeafe; color: #1e40af; }
.status-tag[data-status='收尾'] { background: #ede9fe; color: #5b21b6; }
.status-tag[data-status='已关闭'], .status-tag[data-status='通行'] { background: #e2e8f0; color: #475569; }
.status-tag[data-status='封控'] { background: #fee2e2; color: #b42318; }
.status-tag[data-status='限速'] { background: #ffedd5; color: #9a3412; }
.conflict-tag { color: #b42318; font-size: 12px; margin-left: 8px; }
.queue-panel { margin-top: 16px; }
.queue-panel h3 { font-size: 14px; margin: 0 0 8px; }
.op-no { font-family: monospace; font-size: 12px; }

.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: flex-start; justify-content: center; padding: 32px 16px;
  z-index: 50; overflow-y: auto;
}
.modal {
  background: #fff; border-radius: 10px; width: 640px; max-width: 100%;
  padding: 18px 20px; box-shadow: 0 12px 40px rgba(15, 23, 42, 0.25);
}
.modal-wide { width: 1080px; }
.modal-narrow { width: 460px; }
.modal-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.modal-head h3 { margin: 0 0 4px; font-size: 16px; }
.modal-foot { margin-top: 14px; text-align: right; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0; }
.form-grid label span, .dynamic-row input, .dynamic-row select { font-size: 13px; }
.form-grid label span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 2px; }
.form-grid input, .form-grid select { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.span2 { grid-column: span 2; }
.sub-head { display: flex; justify-content: space-between; align-items: center; margin: 12px 0 6px; }
.sub-head h4 { font-size: 13px; margin: 0; color: #334155; }
.dynamic-row { display: flex; gap: 8px; margin-bottom: 6px; }
.dynamic-row input, .dynamic-row select { flex: 1; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.btn.small { padding: 3px 10px; font-size: 12px; }
.detail-section { margin-top: 18px; border-top: 1px dashed var(--border); padding-top: 12px; }
.detail-section h4 { font-size: 14px; margin: 0 0 8px; display: flex; justify-content: space-between; align-items: center; }
.road-group { border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 10px; }
.road-group-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.window-strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
.window-card { border: 1px solid var(--border); border-radius: 6px; padding: 8px; background: #f8fafc; }
.window-card.closed { background: #f1f5f9; opacity: 0.75; }
.window-time { font-size: 12px; color: var(--muted); }
.window-name { font-size: 13px; margin: 4px 0; }
.window-meta { font-size: 12px; color: #475569; }
.window-card .btn { margin-top: 6px; width: 100%; }
.ledger-title { font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
.conflict-box {
  background: #fef2f2; border: 1px solid #fca5a5; color: #b42318;
  border-radius: 6px; padding: 8px 10px; font-size: 13px;
}
.pick-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }
.pick-item { font-size: 13px; display: flex; gap: 6px; align-items: center; }
.pick-item.disabled { color: var(--muted); }
</style>
