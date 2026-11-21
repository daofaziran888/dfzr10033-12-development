<template>
  <section class="page" data-module="fed_dispatch">
    <header class="page-head">
      <div>
        <h2>受控联邦分发</h2>
        <p class="page-desc">
          从联邦查询缓存的多来源明细按归属切分可共享明细；接收方只能领取获准来源的只读范围，
          许可版本化、撤回在领取时重新核权。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">批次总数</span>
        <strong class="stat-value">{{ rows.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">覆盖冲突</span>
        <strong class="stat-value">{{ conflictCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已领取</span>
        <strong class="stat-value">{{ claimedCount }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>缓存条目ID</span>
        <input v-model="cacheIdInput" data-testid="cache-id" placeholder="result_cache 条目 id" />
      </label>
      <label class="filter-item">
        <span>接收方</span>
        <input v-model="receiverInput" data-testid="receiver" placeholder="接收方名称" />
      </label>
      <label class="filter-item">
        <span>申请来源</span>
        <input v-model="sourcesInput" data-testid="sources" placeholder="逗号分隔，如 SRC-A,SRC-B" />
      </label>
      <button class="btn primary" data-testid="create-application" type="button" @click="apply">
        发起分发申请
      </button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>分发操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="precheck(Number(row.id))">交付预检</button>
            <button class="link" type="button" @click="publish(Number(row.id))">发布</button>
            <button class="link" type="button" @click="claim(Number(row.id))">领取</button>
            <button class="link" type="button" @click="revoke(Number(row.id))">撤回来源</button>
            <button class="link" type="button" @click="showHistory(Number(row.id))">许可/交付历史</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">暂无分发批次</td>
        </tr>
      </tbody>
    </table>

    <div v-if="active" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal modal-wide">
        <h3>批次 {{ active.batchId }} · {{ active.title }}</h3>

        <div v-if="active.error" class="error-text">{{ active.error }}</div>

        <template v-if="active.precheck">
          <h4>交付预检（许可版本 {{ active.precheck.license_version }}）</h4>
          <p>
            获准来源：{{ active.precheck.granted_sources.join(', ') || '无' }} ｜
            撤回来源：{{ active.precheck.revoked_sources.join(', ') || '无' }} ｜
            可交付行数：{{ active.precheck.deliverable_rows }} ｜
            覆盖冲突：{{ active.precheck.coverage_conflict ? '是' : '否' }}
          </p>
          <table class="data-table inner">
            <thead>
              <tr><th>分组</th><th>行数</th><th>小计</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in active.precheck.subtotals" :key="s.category">
                <td>{{ s.category }}</td>
                <td>{{ s.row_count }}</td>
                <td>{{ s.subtotal }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-if="active.delivery">
          <h4>领取结果（许可版本 {{ active.delivery.license_version }}）</h4>
          <p>
            实际可读 {{ active.delivery.row_count }} 行 ｜
            获准：{{ active.delivery.granted_sources.join(', ') || '无' }} ｜
            撤回：{{ active.delivery.revoked_sources.join(', ') || '无' }}
          </p>
          <table class="data-table inner">
            <thead>
              <tr><th>row_id</th><th>category</th><th>value</th><th>来源归属</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in active.delivery.rows" :key="String(r.row_id)">
                <td>{{ r.row_id }}</td>
                <td>{{ r.category }}</td>
                <td>{{ r.value }}</td>
                <td>{{ (Array.isArray(r._sources) ? r._sources : []).join(', ') }}</td>
              </tr>
            </tbody>
          </table>
          <table class="data-table inner">
            <thead>
              <tr><th>分组小计</th><th>行数</th><th>金额</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in active.delivery.subtotals" :key="s.category">
                <td>{{ s.category }}</td>
                <td>{{ s.row_count }}</td>
                <td>{{ s.subtotal }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <template v-if="active.licenseHistory">
          <h4>许可变更历史</h4>
          <ul class="history-list">
            <li v-for="h in active.licenseHistory.history" :key="h.version">
              v{{ h.version }} · {{ h.action }} · 来源：{{ h.sources.join(', ') }} · {{ h.at }}
            </li>
          </ul>
        </template>

        <template v-if="active.deliveries">
          <h4>历史交付（保留授权版本与实际数据列）</h4>
          <ul class="history-list">
            <li v-for="d in active.deliveries.deliveries" :key="d.delivery_id">
              {{ d.claimed_at }} · {{ d.license_version }} · {{ d.row_count }} 行 ·
              列：{{ d.columns.join(', ') }} · 校验 {{ d.checksum }}
            </li>
          </ul>
        </template>

        <div class="page-actions">
          <button class="btn" type="button" @click="active = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ rows.length }} 个分发批次</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  claimDispatch,
  createDispatchApplication,
  getDispatchDeliveries,
  getDispatchLicenseHistory,
  precheckDispatch,
  publishDispatch,
  revokeDispatchSource,
  type DeliveryRecord,
} from '@/api/fed-dispatch'
import { moduleMeta } from '@/data/modules'
import type { EntryRow } from '@/data/types'

const meta = moduleMeta('fed_dispatch')
const rows = ref<EntryRow[]>([])
const errorMessage = ref('')
const cacheIdInput = ref('')
const receiverInput = ref('')
const sourcesInput = ref('')

type DetailState = {
  batchId: number
  title: string
  error: string
  precheck?: Awaited<ReturnType<typeof precheckDispatch>>
  delivery?: DeliveryRecord
  licenseHistory?: Awaited<ReturnType<typeof getDispatchLicenseHistory>>
  deliveries?: Awaited<ReturnType<typeof getDispatchDeliveries>>
}
const active = ref<DetailState | null>(null)

const conflictCount = computed(() => rows.value.filter((r) => r.status === '覆盖冲突').length)
const claimedCount = computed(() => rows.value.filter((r) => r.status === '已领取').length)

async function reload() {
  const payload = await listEntries('fed_dispatch')
  rows.value = payload.items
}

async function apply() {
  errorMessage.value = ''
  const cacheId = Number(cacheIdInput.value)
  const sources = sourcesInput.value.split(',').map((s) => s.trim()).filter(Boolean)
  if (!cacheId || !receiverInput.value || !sources.length) {
    errorMessage.value = '请填写缓存条目ID、接收方和至少一个申请来源'
    return
  }
  try {
    await createDispatchApplication({
      cache_id: cacheId,
      receiver: receiverInput.value,
      requested_sources: sources,
    })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发起申请失败'
  }
}

function openDetail(row: EntryRow): DetailState {
  const state: DetailState = {
    batchId: Number(row.id),
    title: String(row['批次编号'] ?? ''),
    error: '',
  }
  active.value = state
  return state
}

async function precheck(id: number) {
  const row = rows.value.find((r) => Number(r.id) === id)
  const state = openDetail(row as EntryRow)
  try {
    state.precheck = await precheckDispatch(id)
    await reload()
  } catch (error) {
    state.error = error instanceof Error ? error.message : '预检失败'
  }
}

async function publish(id: number) {
  errorMessage.value = ''
  try {
    await publishDispatch(id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发布失败'
  }
}

async function claim(id: number) {
  const row = rows.value.find((r) => Number(r.id) === id)
  const state = openDetail(row as EntryRow)
  try {
    const result = await claimDispatch(id)
    state.delivery = result.delivery
    await reload()
  } catch (error) {
    state.error = error instanceof Error ? error.message : '领取失败'
  }
}

async function revoke(id: number) {
  const source = window.prompt('输入要撤回授权的来源编号，例如 SRC-B')
  if (!source) return
  const row = rows.value.find((r) => Number(r.id) === id)
  const state = openDetail(row as EntryRow)
  try {
    await revokeDispatchSource(id, source.trim())
    state.licenseHistory = await getDispatchLicenseHistory(id)
    await reload()
  } catch (error) {
    state.error = error instanceof Error ? error.message : '撤回失败'
  }
}

async function showHistory(id: number) {
  const row = rows.value.find((r) => Number(r.id) === id)
  const state = openDetail(row as EntryRow)
  try {
    const [licenseHistory, deliveries] = await Promise.all([
      getDispatchLicenseHistory(id),
      getDispatchDeliveries(id),
    ])
    state.licenseHistory = licenseHistory
    state.deliveries = deliveries
  } catch (error) {
    state.error = error instanceof Error ? error.message : '历史读取失败'
  }
}

onMounted(reload)
</script>
