<template>
  <section class="page snow-board" data-module="snow_command">
    <header class="page-head">
      <div>
        <h2>风雪事件指挥板</h2>
        <p class="page-desc">
          气象时段、封控路段与车辆位置按同一风雪事件编排为连续窗口；可从封控段直接调用材料与班组，
          调度结论同时写入作业台账、路段阶段与物资清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="openVehicles">车辆位置板</button>
        <button class="btn" type="button" @click="openOrders">应急指挥令</button>
        <button :class="['btn', queue.length ? 'primary' : '']" type="button" @click="manualFlush">
          断网队列{{ queue.length ? `（${queue.length}）` : '' }}
        </button>
        <button class="btn primary" type="button" @click="showCreate = true">建立风雪事件</button>
      </div>
    </header>

    <div class="net-bar">
      <span :class="['net-dot', online ? 'on' : 'off']"></span>
      <span>{{ online ? '在线：写操作直送，全部带操作号' : '断网：写操作进入本地队列排队，复位后幂等续传' }}</span>
      <button v-if="online && queue.length" class="link" type="button" @click="manualFlush">立即续传</button>
    </div>

    <div class="snow-layout">
      <!-- 左：事件列表 -->
      <aside class="event-list">
        <h3>风雪事件</h3>
        <button
          v-for="event in events"
          :key="event.id"
          :class="['event-item', selectedId === event.id ? 'active' : '']"
          type="button"
          @click="selectEvent(event.id)"
        >
          <div class="event-item-head">
            <strong>{{ event['事件编号'] }}</strong>
            <span :class="['status-tag', statusClass(event['状态'])]">{{ event['状态'] }}</span>
          </div>
          <div class="event-item-name">{{ event['事件名称'] }}</div>
          <div class="event-item-time">{{ event['开始时间'] }} → {{ event['结束时间'] }}</div>
        </button>
      </aside>

      <!-- 右：指挥板主体 -->
      <div class="board-main" v-if="board">
        <div class="board-head">
          <div>
            <h3>{{ board.event['事件名称'] }}（{{ board.event['事件编号'] }}）</h3>
            <p class="weather-baseline">
              <span class="baseline-label">天气基准（冻结）：</span>{{ board.event['天气基准'] }}
            </p>
          </div>
          <div class="event-flow">
            <template v-for="(step, idx) in statusFlow" :key="step">
              <span :class="['flow-step', stepIndex(board.event['状态']) >= idx ? 'reached' : '',
                               board.event['状态'] === step ? 'current' : '']">{{ step }}</span>
              <span v-if="idx < statusFlow.length - 1" class="flow-arrow">→</span>
            </template>
          </div>
        </div>

        <div class="board-actions">
          <button
            v-for="action in availableEventActions(board.event['状态'])"
            :key="action"
            class="btn primary"
            type="button"
            @click="runEventAction(action)"
          >{{ action }}</button>
          <button class="btn ghost" type="button" @click="checkReferences">核对三处引用</button>
          <span v-if="referenceResult" class="reference-line" :class="{ bad: !referenceResult['一致'] }">
            {{ referenceResult['一致'] ? '台账 / 路段阶段 / 物资清单均引用同一事件 ✓' : '发现引用不一致！' }}
          </span>
        </div>

        <!-- 封控段（作业路段） -->
        <h4 class="block-title">作业路段与封控段（可直接调用材料、班组）</h4>
        <div class="segment-grid">
          <article v-for="segment in board.segments" :key="segment.id" class="segment-card">
            <div class="segment-head">
              <div>
                <strong>{{ segment['路段名称'] }}</strong>
                <span class="muted"> {{ segment['路段编号'] }} · {{ segment['起止桩号'] || '桩号未录' }}</span>
              </div>
              <span :class="['status-tag', statusClass(segment['阶段'])]">{{ segment['阶段'] }}</span>
            </div>
            <p class="segment-conclusion" v-if="segment['阶段结论']">阶段结论：{{ segment['阶段结论'] }}</p>

            <!-- 防汛冲突与指挥令 -->
            <div v-if="segment['防汛冲突'].length" class="conflict-box">
              <span class="conflict-warn">与防汛响应同占一路：
                {{ segment['防汛冲突'].map((c: any) => c['记录编号']).join('、') }}</span>
              <template v-if="segment['指挥令']">
                <span :class="['order-pill', segment['指挥令']['优先方'] === '除雪' ? 'snow' : 'flood']">
                  指挥令 {{ segment['指挥令']['令号'] }}：{{ segment['指挥令']['优先方'] }}优先
                </span>
              </template>
              <span v-else class="conflict-warn">尚无生效指挥令，除雪派发将被拦截</span>
            </div>

            <!-- 车辆位置 -->
            <div class="vehicle-row">
              <span class="muted">车辆位置：</span>
              <span v-if="!segment['车辆位置'].length" class="muted">暂无占用车辆</span>
              <span v-for="lock in segment['车辆位置']" :key="lock.id" class="vehicle-pill">
                🚛 {{ lock['车牌号'] }}（{{ lock['任务编号'] }}）
              </span>
            </div>

            <!-- 段内任务 -->
            <ul class="task-list">
              <li v-for="task in segment['任务']" :key="task.id">
                <span class="task-no">{{ task['任务编号'] }}</span>
                <span>{{ task['作业班组'] }} · {{ task['车牌号'] }}</span>
                <span class="muted">{{ task['材料摘要'] }}</span>
                <span :class="['status-tag', taskClass(task['任务状态'])]">{{ task['任务状态'] }}</span>
                <span v-if="task['指挥令号']" class="order-ref">依 {{ task['指挥令号'] }}</span>
                <span class="task-btns">
                  <button v-if="task['任务状态'] === '已派发'" class="link" type="button"
                          @click="runTaskAction(task, '开始作业')">开始作业</button>
                  <button v-if="task['任务状态'] !== '已完成'" class="link" type="button"
                          @click="openConclusion(segment, task)">记录结论</button>
                  <button v-if="task['任务状态'] !== '已完成'" class="link" type="button"
                          @click="runTaskAction(task, '完成任务')">完成任务</button>
                </span>
              </li>
            </ul>

            <!-- 从封控段直接调用材料与班组 -->
            <button
              class="btn primary small"
              type="button"
              :disabled="segment['阶段'] !== '执行中'"
              :title="segment['阶段'] === '执行中' ? '' : '仅执行中封控段可调用材料班组'"
              @click="openDispatch(segment)"
            >调用材料与班组</button>
            <span class="segment-flow">
              <button v-for="action in availableSegmentActions(segment['阶段'])" :key="action"
                      class="link" type="button" @click="runSegmentAction(segment, action)">{{ action }}</button>
            </span>
          </article>
        </div>

        <!-- 连续事件窗口 -->
        <h4 class="block-title">连续事件窗口（气象时段 → 作业路段 → 车辆位置）</h4>
        <div class="timeline">
          <div v-for="(window, idx) in board.windows" :key="idx"
               :class="['timeline-item', windowTypeClass(window['窗口类型'])]">
            <span class="timeline-type">{{ window['窗口类型'] }}</span>
            <span class="timeline-title">{{ window['标题'] }}</span>
            <span class="timeline-time">{{ window['开始'] || '未开始' }} → {{ window['结束'] || '进行中' }}</span>
            <span class="timeline-status">{{ window['状态'] }}</span>
          </div>
        </div>

        <!-- 除雪作业台账 -->
        <h4 class="block-title">
          除雪作业台账 <span class="muted">（引用 {{ board.event['事件编号'] }}，按当时天气基准保留）</span>
        </h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>台账编号</th><th>作业路段</th><th>班组</th><th>车辆</th><th>材料明细</th>
              <th>调度结论</th><th>状态</th><th>天气基准</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ledger in board.ledgers" :key="ledger.id">
              <td>{{ ledger['台账编号'] }}</td>
              <td>{{ ledger['作业路段'] }}</td>
              <td>{{ ledger['作业班组'] }}</td>
              <td>{{ ledger['作业车辆'] }}</td>
              <td>{{ ledger['材料明细'] }}</td>
              <td>{{ ledger['调度结论'] || '—' }}</td>
              <td>{{ ledger['作业状态'] }}</td>
              <td class="baseline-cell">{{ ledger['天气基准'] }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 物资清单 -->
        <h4 class="block-title">物资清单（同一事件引用）</h4>
        <table class="data-table">
          <thead>
            <tr><th>清单编号</th><th>材料</th><th>库存</th><th>累计耗用</th><th>变动留痕</th></tr>
          </thead>
          <tbody>
            <tr v-for="material in board.materials" :key="material.id">
              <td>{{ material['清单编号'] }}</td>
              <td>{{ material['材料名称'] }}</td>
              <td>{{ material['库存量'] }}{{ material['单位'] }}</td>
              <td>{{ material['累计耗用'] }}{{ material['单位'] }}</td>
              <td class="log-cell">
                <span v-for="(entry, i) in material['变动日志']" :key="i" class="log-line">
                  {{ entry['时间'] }} {{ entry['说明'] }}<template v-if="entry['数量'] !== undefined">（{{ entry['数量'] }}）</template>
                </span>
                <span v-if="!material['变动日志'].length" class="muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-else class="board-empty">从左侧选择一个风雪事件查看指挥板，或新建事件。</div>
    </div>

    <!-- 建立事件 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <div class="modal">
        <h3>建立风雪事件</h3>
        <label class="form-row"><span>事件名称</span>
          <input v-model="createForm['事件名称']" placeholder="如：寒潮风雪过程" /></label>
        <div class="form-row2">
          <label><span>气象开始</span><input v-model="createForm['开始时间']" type="datetime-local" /></label>
          <label><span>气象结束</span><input v-model="createForm['结束时间']" type="datetime-local" /></label>
        </div>
        <label class="form-row"><span>预警级别</span>
          <select v-model="createForm['预警级别']">
            <option>蓝色</option><option>黄色</option><option>橙色</option><option>红色</option>
          </select></label>
        <label class="form-row"><span>天气基准（冻结为历史口径）</span>
          <textarea v-model="createForm['天气基准']" rows="2"
                    placeholder="如：暴雪橙色预警；气温-8℃；累计降雪15cm（气象台口径）"></textarea></label>

        <div class="form-block">
          <span class="form-block-title">初始封控段</span>
          <div v-for="(row, idx) in createForm.segments" :key="idx" class="inline-row">
            <input v-model="row['路段名称']" placeholder="路段名称" />
            <input v-model="row['起止桩号']" placeholder="起止桩号（选填）" />
            <button class="link danger" type="button" @click="createForm.segments.splice(idx, 1)">删除</button>
          </div>
          <button class="btn small" type="button" @click="createForm.segments.push({})">加封控段</button>
        </div>

        <div class="form-block">
          <span class="form-block-title">初始物资清单</span>
          <div v-for="(row, idx) in createForm.materials" :key="idx" class="inline-row">
            <input v-model="row['材料名称']" placeholder="材料名称" />
            <input v-model.number="row['库存量']" type="number" placeholder="库存" style="width: 90px" />
            <input v-model="row['单位']" placeholder="单位" style="width: 80px" />
            <button class="link danger" type="button" @click="createForm.materials.splice(idx, 1)">删除</button>
          </div>
          <button class="btn small" type="button" @click="createForm.materials.push({ 库存量: 0, 单位: '吨' })">
            加材料
          </button>
        </div>

        <div class="modal-foot">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="button" :disabled="creating" @click="submitCreate">
            {{ creating ? '提交中…' : '建立并编排窗口' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 派发：从封控段调用车辆、班组、材料 -->
    <div v-if="dispatchTarget" class="modal-mask" @click.self="dispatchTarget = null">
      <div class="modal">
        <h3>调用材料与班组 · {{ dispatchTarget['路段名称'] }}</h3>
        <p class="muted">
          任务派发、车辆占用、材料扣减在同一事务内完成；与防汛冲突时必须依据生效指挥令裁决。
        </p>
        <label class="form-row"><span>作业班组</span>
          <input v-model="dispatchForm['作业班组']" placeholder="如：一道班（8人）" /></label>
        <label class="form-row"><span>作业车辆</span>
          <select v-model.number="dispatchForm['车辆id']">
            <option :value="undefined">请选择可用车辆</option>
            <option v-for="vehicle in availableVehicles" :key="vehicle.id" :value="vehicle.id">
              {{ vehicle['车牌号'] }} · {{ vehicle['车辆类型'] }}（{{ vehicle['status'] }}）
            </option>
          </select></label>
        <div class="form-block">
          <span class="form-block-title">调用材料（从事件物资清单）</span>
          <div v-for="(row, idx) in dispatchForm['材料']" :key="idx" class="inline-row">
            <select v-model.number="row['清单id']" style="flex: 2">
              <option :value="undefined">选择材料</option>
              <option v-for="material in board!.materials" :key="material.id" :value="material.id">
                {{ material['材料名称'] }}（库存 {{ material['库存量'] }}{{ material['单位'] }}）
              </option>
            </select>
            <input v-model.number="row['数量']" type="number" min="0" step="0.1" placeholder="数量" style="width: 110px" />
            <button class="link danger" type="button" @click="dispatchForm['材料'].splice(idx, 1)">删除</button>
          </div>
          <button class="btn small" type="button" @click="dispatchForm['材料'].push({})">加材料</button>
        </div>
        <div class="modal-foot">
          <button class="btn" type="button" @click="dispatchTarget = null">取消</button>
          <button class="btn primary" type="button" :disabled="dispatching" @click="submitDispatch">
            {{ dispatching ? '事务提交中…' : '确认派发（事务）' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 调度会话结论 -->
    <div v-if="conclusionTarget" class="modal-mask" @click.self="conclusionTarget = null">
      <div class="modal">
        <h3>调度会话结论 · {{ conclusionTarget.segment['路段名称'] }}</h3>
        <p class="muted">一次写入除雪作业台账、路段阶段、物资清单三处，全部引用 {{ board?.event['事件编号'] }}。</p>
        <textarea v-model="conclusionText" rows="4"
                  placeholder="如：22时加撒一次融雪剂，班组轮换，明早6时复评"></textarea>
        <div class="modal-foot">
          <button class="btn" type="button" @click="conclusionTarget = null">取消</button>
          <button class="btn primary" type="button" @click="submitConclusion">写入三处</button>
        </div>
      </div>
    </div>

    <!-- 车辆位置板 -->
    <div v-if="showVehicles" class="modal-mask" @click.self="showVehicles = false">
      <div class="modal wide">
        <h3>车辆位置板</h3>
        <table class="data-table">
          <thead><tr><th>车牌号</th><th>类型</th><th>台账状态</th><th>指挥板占用</th><th>所在事件/路段</th></tr></thead>
          <tbody>
            <tr v-for="vehicle in vehicles" :key="vehicle.id">
              <td>{{ vehicle['车牌号'] }}</td>
              <td>{{ vehicle['车辆类型'] }}</td>
              <td>{{ vehicle['status'] }}</td>
              <td>
                <span :class="['status-tag', vehicle['占用中'] ? 'tag-exec' : 'tag-done']">
                  {{ vehicle['占用中'] ? '占用中' : '空闲' }}
                </span>
              </td>
              <td>{{ vehicle['占用中'] ? `${vehicle['所在事件']} / ${vehicle['所在路段']}` : '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 应急指挥令 -->
    <div v-if="showOrders" class="modal-mask" @click.self="showOrders = false">
      <div class="modal wide">
        <h3>应急指挥令（除雪/防汛同占一路时的优先级依据）</h3>
        <table class="data-table">
          <thead><tr><th>令号</th><th>影响路段</th><th>优先方</th><th>依据</th><th>状态</th><th></th></tr></thead>
          <tbody>
            <tr v-for="order in orders" :key="order.id">
              <td>{{ order['令号'] }}</td>
              <td>{{ order['影响路段'] }}</td>
              <td>
                <span :class="['order-pill', order['优先方'] === '除雪' ? 'snow' : 'flood']">
                  {{ order['优先方'] }}优先
                </span>
              </td>
              <td>{{ order['依据'] }}</td>
              <td>{{ order['状态'] }}</td>
              <td>
                <button v-if="order['状态'] === '生效'" class="link danger" type="button"
                        @click="revokeOrder(order)">撤销</button>
              </td>
            </tr>
          </tbody>
        </table>
        <h4 class="block-title">下达新指挥令</h4>
        <div class="inline-row">
          <input v-model="orderForm['影响路段']" placeholder="影响路段" />
          <select v-model="orderForm['优先方']" style="width: 110px">
            <option value="">优先方</option><option>除雪</option><option>防汛</option>
          </select>
          <input v-model="orderForm['依据']" placeholder="指挥依据" />
          <button class="btn primary small" type="button" @click="submitOrder">下达</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span v-if="message" :class="messageOk ? '' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'
import { flushQueue, queueOps, snowMutate, type QueuedOp } from '@/api/offlineQueue'

type AnyRow = Record<string, any>

interface BoardData {
  event: AnyRow
  segments: AnyRow[]
  tasks: AnyRow[]
  ledgers: AnyRow[]
  materials: AnyRow[]
  windows: AnyRow[]
  orders: AnyRow[]
}

const statusFlow = ['待启动', '执行中', '收尾', '已关闭']

const events = ref<AnyRow[]>([])
const selectedId = ref<number | null>(null)
const board = ref<BoardData | null>(null)
const vehicles = ref<AnyRow[]>([])
const orders = ref<AnyRow[]>([])
const message = ref('')
const messageOk = ref(true)

const online = ref(navigator.onLine)
const queue = ref<QueuedOp[]>([])

const showCreate = ref(false)
const creating = ref(false)
const showVehicles = ref(false)
const showOrders = ref(false)
const dispatchTarget = ref<AnyRow | null>(null)
const dispatching = ref(false)
const conclusionTarget = ref<{ segment: AnyRow; task: AnyRow | null } | null>(null)
const conclusionText = ref('')
const referenceResult = ref<AnyRow | null>(null)

const createForm = reactive<AnyRow>({
  事件名称: '', 开始时间: '', 结束时间: '', 预警级别: '橙色', 天气基准: '',
  segments: [{ 路段名称: '', 起止桩号: '' }],
  materials: [{ 材料名称: '', 库存量: 0, 单位: '吨' }],
})
const dispatchForm = reactive<{ 作业班组: string; 车辆id?: number; 材料: AnyRow[] }>({
  作业班组: '', 车辆id: undefined, 材料: [{ 清单id: undefined, 数量: undefined }],
})
const orderForm = reactive<AnyRow>({ 影响路段: '', 优先方: '', 依据: '' })

const availableVehicles = computed(() => vehicles.value.filter((v) => v.status === '在库' && !v.占用中))

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function stepIndex(status: string): number {
  return Math.max(statusFlow.indexOf(status), 0)
}
function statusClass(status: string): string {
  return { 待启动: 'tag-pending', 执行中: 'tag-exec', 收尾: 'tag-wrap', 已关闭: 'tag-done' }[status] || ''
}
function taskClass(status: string): string {
  return { 已派发: 'tag-pending', 执行中: 'tag-exec', 已完成: 'tag-done' }[status] || ''
}
function windowTypeClass(type: string): string {
  return { 气象时段: 'win-weather', 作业路段: 'win-road', 车辆位置: 'win-truck' }[type] || ''
}
function availableEventActions(status: string): string[] {
  return { 待启动: ['启动事件'], 执行中: ['转入收尾'], 收尾: ['关闭事件'], 已关闭: [] }[status] || []
}
function availableSegmentActions(status: string): string[] {
  return { 待启动: ['启动路段'], 执行中: ['路段收尾'], 收尾: ['关闭路段'], 已关闭: [] }[status] || []
}

async function loadEvents() {
  const data = await fetchJson<{ items: AnyRow[] }>('/api/snow/events')
  events.value = data.items
  if (selectedId.value === null && data.items.length) {
    const preferred = data.items.find((item) => item.状态 !== '已关闭') || data.items[0]
    await selectEvent(preferred.id)
  }
}

async function selectEvent(id: number) {
  selectedId.value = id
  referenceResult.value = null
  try {
    board.value = await fetchJson<BoardData>(`/api/snow/events/${id}`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '指挥板读取失败', false)
  }
}

async function reloadBoard() {
  if (selectedId.value !== null) {
    await selectEvent(selectedId.value)
  }
}

async function mutateAndRefresh(
  path: string,
  values: Record<string, any>,
  label: string,
): Promise<AnyRow> {
  try {
    const result = await snowMutate(path, values, label)
    notify(result.message || (result.queued ? '已进入断网队列' : '操作已生效'), true)
    await reloadBoard()
    await loadEvents()
    return result
  } catch (error) {
    notify(error instanceof Error ? error.message : `${label}失败`, false)
    return { ok: false }
  }
}

async function submitCreate() {
  creating.value = true
  try {
    const payload: AnyRow = { ...createForm }
    payload.segments = createForm.segments.filter((row: AnyRow) => row.路段名称?.trim())
    payload.materials = createForm.materials.filter((row: AnyRow) => row.材料名称?.trim())
    const result = await mutateAndRefresh('/api/snow/events', payload, '建立风雪事件')
    if (result.ok && !result.queued && result.event) {
      showCreate.value = false
      await selectEvent(result.event.id)
    }
    if (result.queued) showCreate.value = false
  } finally {
    creating.value = false
  }
}

async function runEventAction(action: string) {
  if (!board.value) return
  await mutateAndRefresh(
    `/api/snow/events/${board.value.event.id}/actions`, { action }, `事件${action}`,
  )
}

async function runSegmentAction(segment: AnyRow, action: string) {
  if (!board.value) return
  await mutateAndRefresh(
    `/api/snow/events/${board.value.event.id}/segments/${segment.id}/actions`,
    { action }, `路段${action}`,
  )
}

function openDispatch(segment: AnyRow) {
  dispatchTarget.value = segment
  dispatchForm.作业班组 = ''
  dispatchForm.车辆id = undefined
  dispatchForm.材料 = [{ 清单id: undefined, 数量: undefined }]
  void loadVehicles()
}

async function submitDispatch() {
  if (!board.value || !dispatchTarget.value) return
  const materials = dispatchForm.材料
    .filter((row) => row.清单id !== undefined && Number(row.数量) > 0)
  dispatching.value = true
  try {
    const result = await mutateAndRefresh(
      `/api/snow/events/${board.value.event.id}/dispatches`,
      {
        路段id: dispatchTarget.value.id,
        作业班组: dispatchForm.作业班组,
        车辆id: dispatchForm.车辆id,
        材料: materials,
      },
      '派发除雪任务',
    )
    if (result.ok && !result.queued) dispatchTarget.value = null
    if (result.queued) dispatchTarget.value = null
  } finally {
    dispatching.value = false
  }
}

async function runTaskAction(task: AnyRow, action: string) {
  if (!board.value) return
  await mutateAndRefresh(
    `/api/snow/events/${board.value.event.id}/tasks/${task.id}/actions`,
    { action }, `任务${action}`,
  )
}

function openConclusion(segment: AnyRow, task: AnyRow | null) {
  conclusionTarget.value = { segment, task }
  conclusionText.value = ''
}

async function submitConclusion() {
  if (!board.value || !conclusionTarget.value) return
  const text = conclusionText.value.trim()
  if (!text) {
    notify('请填写调度会话结论', false)
    return
  }
  const result = await mutateAndRefresh(
    `/api/snow/events/${board.value.event.id}/conclusions`,
    { 路段id: conclusionTarget.value.segment.id,
      任务id: conclusionTarget.value.task?.id ?? null,
      调度结论: text },
    '写入调度结论',
  )
  if (result.ok) conclusionTarget.value = null
}

async function checkReferences() {
  if (!board.value) return
  try {
    referenceResult.value = await fetchJson<AnyRow>(
      `/api/snow/events/${board.value.event.id}/references`,
    )
    notify(referenceResult.value.一致 ? '三处引用核对一致' : '存在引用不一致！', referenceResult.value.一致)
  } catch (error) {
    notify(error instanceof Error ? error.message : '引用核对失败', false)
  }
}

async function loadVehicles() {
  vehicles.value = (await fetchJson<{ items: AnyRow[] }>('/api/snow/vehicles')).items
}
async function openVehicles() {
  await loadVehicles()
  showVehicles.value = true
}

async function loadOrders() {
  orders.value = (await fetchJson<{ items: AnyRow[] }>('/api/snow/orders')).items
}
async function openOrders() {
  await loadOrders()
  showOrders.value = true
}
async function submitOrder() {
  const result = await mutateAndRefresh('/api/snow/orders', { ...orderForm }, '下达应急指挥令')
  if (result.ok) {
    orderForm.影响路段 = ''
    orderForm.优先方 = ''
    orderForm.依据 = ''
    await loadOrders()
  }
}
async function revokeOrder(order: AnyRow) {
  await mutateAndRefresh(`/api/snow/orders/${order.id}/revoke`, {}, `撤销指挥令${order.令号}`)
  await loadOrders()
}

// ---- 断网监听与复位自动续传 ----
function refreshQueue() {
  queue.value = queueOps()
}
async function handleOnline() {
  online.value = true
  refreshQueue()
  if (queue.value.length) {
    notify('网络复位，开始按操作号幂等续传…', true)
    try {
      const result = await flushQueue()
      notify(result.message, true)
      refreshQueue()
      await reloadBoard()
      await loadEvents()
    } catch (error) {
      notify(error instanceof Error ? error.message : '续传失败', false)
    }
  }
}
function handleOffline() {
  online.value = false
}
async function manualFlush() {
  if (!queue.value.length) {
    notify('本地队列是空的', true)
    return
  }
  if (!navigator.onLine) {
    notify('仍处于断网状态，操作已安全排队，联网后会自动续传', false)
    return
  }
  try {
    const result = await flushQueue()
    const detail = result.results
      .map((item, idx) => `第${idx + 1}笔：${item.ok ? '成功' : item.message}`)
      .join('；')
    notify(`${result.message} ${detail}`, result.remaining.length === 0)
    refreshQueue()
    await reloadBoard()
  } catch (error) {
    notify(error instanceof Error ? error.message : '续传失败', false)
  }
}

// 请求头里的 request 已带错误封装，这里仅为让 no-unused 不报警（保留统一封装引用）
void request

onMounted(async () => {
  refreshQueue()
  window.addEventListener('online', handleOnline)
  window.addEventListener('offline', handleOffline)
  await loadVehicles()
  await loadEvents()
})
onUnmounted(() => {
  window.removeEventListener('online', handleOnline)
  window.removeEventListener('offline', handleOffline)
})
</script>

<style scoped>
.snow-board { display: flex; flex-direction: column; }
.page-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.net-bar { display: flex; align-items: center; gap: 8px; font-size: 12px;
  background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 10px; margin-bottom: 12px; color: var(--muted); }
.net-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.net-dot.on { background: #12b76a; }
.net-dot.off { background: #f04438; }

.snow-layout { display: flex; gap: 12px; align-items: flex-start; }
.event-list { width: 260px; flex-shrink: 0; background: #fff;
  border: 1px solid var(--border); border-radius: 8px; padding: 10px; }
.event-list h3 { margin: 0 0 8px; font-size: 14px; }
.event-item { display: block; width: 100%; text-align: left; border: 1px solid var(--border);
  border-radius: 6px; background: #fafafa; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.event-item.active { border-color: var(--brand); box-shadow: 0 0 0 1px var(--brand) inset; }
.event-item-head { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
.event-item-name { font-size: 13px; margin: 4px 0 2px; }
.event-item-time { font-size: 11px; color: var(--muted); }

.board-main { flex: 1; min-width: 0; }
.board-empty { flex: 1; background: #fff; border: 1px dashed var(--border);
  border-radius: 8px; padding: 40px; text-align: center; color: var(--muted); }
.board-head { display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.board-head h3 { margin: 0 0 6px; font-size: 16px; }
.weather-baseline { margin: 0; font-size: 12px; color: #344054; max-width: 560px; }
.baseline-label { color: var(--muted); }
.event-flow { display: flex; align-items: center; gap: 4px; font-size: 12px; }
.flow-step { padding: 3px 8px; border-radius: 12px; background: #f2f4f7; color: var(--muted); }
.flow-step.reached { background: #e0f2fe; color: #0369a1; }
.flow-step.current { background: var(--brand); color: #fff; }
.flow-arrow { color: var(--muted); }

.board-actions { display: flex; align-items: center; gap: 8px; margin: 10px 0; flex-wrap: wrap; }
.reference-line { font-size: 12px; color: #027a48; }
.reference-line.bad { color: #b42318; }

.block-title { font-size: 14px; margin: 18px 0 8px; }
.muted { color: var(--muted); font-size: 12px; font-weight: normal; }

.segment-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; }
.segment-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.segment-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.segment-conclusion { font-size: 12px; margin: 6px 0; color: #344054; }
.conflict-box { background: #fffaeb; border: 1px solid #fedf89; border-radius: 6px;
  padding: 6px 8px; font-size: 12px; margin: 6px 0; display: flex; flex-direction: column; gap: 4px; }
.conflict-warn { color: #b54708; }
.order-pill { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 10px; }
.order-pill.snow { background: #e0f2fe; color: #0369a1; }
.order-pill.flood { background: #d1fadf; color: #027a48; }
.vehicle-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; margin: 6px 0; }
.vehicle-pill { background: #f2f4f7; border-radius: 10px; padding: 1px 8px; font-size: 12px; }
.task-list { list-style: none; padding: 0; margin: 6px 0; font-size: 12px;
  display: flex; flex-direction: column; gap: 4px; }
.task-list li { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.task-no { font-weight: 600; }
.task-btns { margin-left: auto; display: flex; gap: 8px; }
.order-ref { color: #b54708; font-size: 11px; }
.segment-flow { display: flex; gap: 10px; margin-left: 8px; }

.status-tag { font-size: 11px; padding: 1px 8px; border-radius: 10px; background: #f2f4f7; color: #475467; }
.tag-pending { background: #f2f4f7; color: #475467; }
.tag-exec { background: #fee4e2; color: #b42318; }
.tag-wrap { background: #fef0c7; color: #b54708; }
.tag-done { background: #d1fadf; color: #027a48; }

.timeline { display: flex; flex-direction: column; gap: 6px; }
.timeline-item { display: grid; grid-template-columns: 80px 1fr 280px 80px; gap: 10px;
  background: #fff; border-left: 4px solid var(--brand); border-radius: 4px;
  padding: 6px 10px; font-size: 12px; align-items: center; }
.timeline-item.win-weather { border-left-color: #7e57c2; }
.timeline-item.win-road { border-left-color: var(--brand); }
.timeline-item.win-truck { border-left-color: #12b76a; }
.timeline-type { color: var(--muted); }
.timeline-time { color: var(--muted); font-size: 11px; text-align: right; }
.timeline-status { text-align: right; }

.baseline-cell, .log-cell { font-size: 11px; color: #475467; max-width: 260px; }
.log-cell { display: flex; flex-direction: column; gap: 2px; }
.log-line { display: block; }

.btn.small, .btn:disabled { padding: 4px 10px; font-size: 12px; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.link.danger { color: #b42318; }

.modal-mask { position: fixed; inset: 0; background: rgba(16, 24, 40, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 50; padding: 20px; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 560px;
  max-height: 86vh; overflow: auto; }
.modal.wide { width: 820px; }
.modal h3 { margin: 0 0 10px; font-size: 15px; }
.modal textarea { width: 100%; border: 1px solid var(--border); border-radius: 6px;
  padding: 8px; font-size: 13px; resize: vertical; }
.form-row { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 12px; color: var(--muted); }
.form-row2 { display: flex; gap: 10px; margin-bottom: 10px; }
.form-row2 label { flex: 1; display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-row input, .form-row select, .form-row textarea,
.form-row2 input, .inline-row input, .inline-row select {
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; font-size: 13px;
}
.form-block { border-top: 1px dashed var(--border); padding-top: 8px; margin: 10px 0; }
.form-block-title { font-size: 12px; color: var(--muted); display: block; margin-bottom: 6px; }
.inline-row { display: flex; gap: 8px; margin-bottom: 6px; }
.inline-row input, .inline-row select { flex: 1; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
</style>
