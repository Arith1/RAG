<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, type Ref } from 'vue'
import { api, type BatchUploadResult, type DocItem, type QueueDeadItem, type QueueInflightItem, type QueuePendingItem, type QueueStats } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'
import StatusBadge from '../components/StatusBadge.vue'

const auth = useAuthStore()
const feedback = useFeedback()
const docs = ref<DocItem[]>([])
const stats = ref<QueueStats | null>(null)
const error = ref('')
const loading = ref(false)
const uploading = ref(false)
const shareOnUpload = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const pendingItems = ref<QueuePendingItem[]>([])
const inflightItems = ref<QueueInflightItem[]>([])
const deadItems = ref<QueueDeadItem[]>([])
const queueOpen = ref(false)
const queueBusy = ref(false)

const isLoggedIn = computed(() => auth.isLoggedIn)

function isMine(d: DocItem) {
  return d.owner_id === auth.user?.id
}

const myDocs = computed(() => docs.value.filter(isMine))
const myPrivate = computed(() => myDocs.value.filter((d) => !d.is_public))
const myShared = computed(() => myDocs.value.filter((d) => d.is_public))

// 文档列表分页：私有 / 共享两组各自分页，pageSize 固定 10 条/页
const pageSize = 10
const privatePage = ref(1)
const sharedPage = ref(1)

interface DocGroup {
  key: string
  title: string
  hint: string
  docs: DocItem[]
  total: number
  page: Ref<number>
  manageable: boolean
  emptyText: string
}

function slicePage(list: DocItem[], page: number): DocItem[] {
  const start = (page - 1) * pageSize
  return list.slice(start, start + pageSize)
}

function totalPages(total: number): number {
  return Math.max(1, Math.ceil(total / pageSize))
}

const groups = computed<DocGroup[]>(() => [
  {
    key: 'private',
    title: '私有文档',
    hint: '仅自己可检索',
    docs: slicePage(myPrivate.value, privatePage.value),
    total: myPrivate.value.length,
    page: privatePage,
    manageable: true,
    emptyText: '还没有私有文档',
  },
  {
    key: 'shared',
    title: '共享文档',
    hint: '其他用户可检索',
    docs: slicePage(myShared.value, sharedPage.value),
    total: myShared.value.length,
    page: sharedPage,
    manageable: true,
    emptyText: '还没有共享文档',
  },
])

function prevPage(g: DocGroup) {
  if (g.page.value > 1) g.page.value--
}

function nextPage(g: DocGroup) {
  if (g.page.value < totalPages(g.total)) g.page.value++
}

function clampPages() {
  const pMax = totalPages(myPrivate.value.length)
  const sMax = totalPages(myShared.value.length)
  if (privatePage.value > pMax) privatePage.value = pMax
  if (sharedPage.value > sMax) sharedPage.value = sMax
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api<{ total: number; items: DocItem[] }>('/api/documents?limit=500')
    docs.value = data.items
    clampPages()
    stats.value = await api<QueueStats>('/api/ingest/stats').catch(() => null)
    const [pending, inflight, dead] = await Promise.all([
      api<QueuePendingItem[]>('/api/ingest/queue?limit=50').catch(() => []),
      api<QueueInflightItem[]>('/api/ingest/inflight').catch(() => []),
      api<QueueDeadItem[]>('/api/ingest/dead?limit=50').catch(() => []),
    ])
    pendingItems.value = pending
    inflightItems.value = inflight
    deadItems.value = dead
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function openPicker() {
  fileInput.value?.click()
}

async function onFileChosen(e: Event) {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  uploading.value = true
  error.value = ''
  const form = new FormData()
  files.forEach((f) => form.append('files', f))
  form.append('is_public', String(shareOnUpload.value))
  try {
    const data = await api<BatchUploadResult>('/api/documents/upload', {
      method: 'POST',
      body: form,
    })
    await load()
    const errs = data.results.filter((r) => r.status === 'error')
    if (errs.length) {
      const detail = errs.map((r) => `${r.file_name}：${r.message}`).join('；')
      feedback.notify(`${data.message}。${detail}`, data.accepted > 0 ? 'success' : 'error')
    } else {
      feedback.notify(data.message, 'success')
    }
    // 异步入库，稍后再刷一次状态
    window.setTimeout(load, 1800)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    uploading.value = false
    input.value = ''
  }
}

async function remove(d: DocItem) {
  const ok = await feedback.confirm({
    message: '确认删除？',
    detail: `确认删除「${d.file_name}」？删除后该文档将从知识库中移除。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  error.value = ''
  try {
    await api(`/api/documents/${d.id}`, { method: 'DELETE' })
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function revoke(d: DocItem) {
  const ok = await feedback.confirm({
    message: '确认撤销共享？',
    detail: `确认撤销「${d.file_name}」的共享？撤销后其他用户将无法检索到它。`,
    confirmText: '撤销共享',
  })
  if (!ok) return
  error.value = ''
  try {
    await api(`/api/documents/${d.id}/revoke`, { method: 'POST' })
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function share(d: DocItem) {
  const ok = await feedback.confirm({
    message: '确认设为公开共享？',
    detail: `确认将「${d.file_name}」设为共享？其他用户将可以检索并下载它。`,
    confirmText: '设为公开',
  })
  if (!ok) return
  error.value = ''
  try {
    await api(`/api/documents/${d.id}/share`, { method: 'POST' })
    await load()
    feedback.notify('已设为公开共享', 'success')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

function download(d: DocItem) {
  const token = auth.token
  fetch(`/api/documents/${d.id}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const ct = res.headers.get('content-type') ?? ''
      if (ct.includes('application/json')) {
        const data = (await res.json()) as { url?: string }
        if (data.url) {
          window.open(data.url, '_blank', 'noopener')
          return
        }
      }
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = d.file_name
      a.click()
      URL.revokeObjectURL(url)
    })
    .catch((e) => {
      error.value = e instanceof Error ? e.message : String(e)
    })
}

async function retryDead(item: QueueDeadItem) {
  queueBusy.value = true
  try {
    await api(`/api/ingest/dead/${item.msg_id}/retry`, { method: 'POST' })
    await load()
    feedback.notify(`已重新提交「${item.file_name}」入库`, 'success')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    queueBusy.value = false
  }
}

async function retryAllDead() {
  const ok = await feedback.confirm({
    message: '重试全部失败任务？',
    detail: `将把死信队列中的 ${deadItems.value.length} 条失败任务重新加入入库队列。`,
    confirmText: '全部重试',
  })
  if (!ok) return
  queueBusy.value = true
  try {
    const res = await api<{ retried: number; failed: number }>('/api/ingest/dead/retry-all', { method: 'POST' })
    await load()
    feedback.notify(`已重新提交 ${res.retried} 条，失败 ${res.failed} 条`, 'success')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    queueBusy.value = false
  }
}

async function clearDead() {
  const ok = await feedback.confirm({
    message: '清空失败队列？',
    detail: '将删除死信队列中的全部失败任务（不影响已入库的文档）。',
    confirmText: '清空',
    danger: true,
  })
  if (!ok) return
  queueBusy.value = true
  try {
    const res = await api<{ cleared: number }>('/api/ingest/dead', { method: 'DELETE' })
    await load()
    feedback.notify(`已清空 ${res.cleared} 条失败任务`, 'success')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    queueBusy.value = false
  }
}

// 定时轮询：入库是异步的，周期刷新文档列表与队列状态（文档列表有 Redis 缓存，开销低）
const QUEUE_POLL_MS = 5000
let pollTimer: number | undefined

onMounted(() => {
  if (isLoggedIn.value) load()
  pollTimer = window.setInterval(() => {
    if (isLoggedIn.value) load()
  }, QUEUE_POLL_MS)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="docs-page">
    <div class="docs-inner">
      <div v-if="!isLoggedIn" class="guest">
        <div class="guest-card">
          <h2>登录后管理你的文档</h2>
          <p>上传文档并让它在知识库中可被检索；也可以把文档共享出来，让其他用户引用。</p>
          <RouterLink to="/login" class="btn btn-primary">去登录</RouterLink>
        </div>
      </div>

      <template v-else>
        <header class="docs-head">
          <div>
            <h1>文档管理</h1>
            <p class="sub">私有与共享分开展示；这里只管理你自己的文档，他人共享请到「知识库」查看。</p>
          </div>
          <div class="head-actions">
            <label class="share-toggle" :title="shareOnUpload ? '本次上传将作为共享文档，其他用户可检索' : '本次上传将作为私有文档'">
              <input v-model="shareOnUpload" type="checkbox" class="switch" />
              <span class="switch-track"><span class="switch-thumb"></span></span>
              <span class="toggle-label">上传后共享</span>
            </label>
            <button class="btn btn-primary" :disabled="uploading" title="支持一次选择多个文件批量上传" @click="openPicker">
              <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
                <path d="M10 4v12M4 10h12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              </svg>
              {{ uploading ? '上传中…' : '上传文档' }}
            </button>
            <input ref="fileInput" type="file" accept=".md,.txt,.docx,.pdf" multiple class="visually-hidden" @change="onFileChosen" />
          </div>
        </header>

        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>

        <div v-if="stats" class="queue-panel">
          <div class="queue-stats-bar">
            <button class="queue-toggle" :aria-expanded="queueOpen" @click="queueOpen = !queueOpen">
              <span class="queue-stats">
                <span class="qs" title="待处理中的入库任务"><span class="dot warn"></span>待处理 {{ stats.pending }}</span>
                <span class="qs" title="正在入库"><span class="dot ok"></span>入库中 {{ stats.inflight }}</span>
                <span class="qs" title="失败/死信"><span class="dot err"></span>失败 {{ stats.dead_letter }}</span>
              </span>
              <svg viewBox="0 0 20 20" width="14" height="14" class="chev" :class="{ up: queueOpen }" aria-hidden="true">
                <path d="M5 8l5 5 5-5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>

          <Transition name="drop">
            <div v-if="queueOpen" class="queue-detail">
              <div class="queue-col">
                <h3>待处理</h3>
                <ul v-if="pendingItems.length" class="queue-list">
                  <li v-for="it in pendingItems" :key="it.msg_id">
                    <span class="qname" :title="it.path">{{ it.file_name }}</span>
                    <span class="qmeta">重试 {{ it.retries }}</span>
                  </li>
                </ul>
                <p v-else class="qempty">暂无待处理任务</p>
              </div>

              <div class="queue-col">
                <h3>入库中</h3>
                <ul v-if="inflightItems.length" class="queue-list">
                  <li v-for="it in inflightItems" :key="it.path">
                    <span class="qname" :title="it.path">{{ it.file_name }}</span>
                  </li>
                </ul>
                <p v-else class="qempty">暂无入库中任务</p>
              </div>

              <div class="queue-col">
                <div class="qcol-head">
                  <h3>失败</h3>
                  <template v-if="auth.isAdmin && deadItems.length">
                    <button class="btn btn-subtle btn-xs" :disabled="queueBusy" @click="retryAllDead">全部重试</button>
                    <button class="btn btn-ghost btn-xs" :disabled="queueBusy" @click="clearDead">清空</button>
                  </template>
                </div>
                <ul v-if="deadItems.length" class="queue-list">
                  <li v-for="it in deadItems" :key="it.msg_id">
                    <div class="qname-row">
                      <span class="qname" :title="it.path">{{ it.file_name }}</span>
                      <button v-if="auth.isAdmin" class="icon-btn retry" :disabled="queueBusy" title="重新入库" @click="retryDead(it)">
                        <svg viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
                          <path d="M16 8a6 6 0 1 0-1.5 4.9M16 8V4m0 4h-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
                        </svg>
                      </button>
                    </div>
                    <span class="qerr" :title="it.error">{{ it.error }}</span>
                  </li>
                </ul>
                <p v-else class="qempty">暂无失败任务</p>
              </div>
            </div>
          </Transition>
        </div>

        <div class="docs-groups">
          <section
            v-for="g in groups"
            :key="g.key"
            class="doc-group"
            :class="{ 'is-other': !g.manageable }"
          >
            <div class="group-head">
              <h2>{{ g.title }}</h2>
              <span class="group-count">{{ g.total }}</span>
              <span class="group-hint">{{ g.hint }}</span>
            </div>

            <div v-if="g.total > pageSize" class="group-pager">
              <button class="btn btn-ghost btn-xs" :disabled="g.page.value <= 1" @click="prevPage(g)">上一页</button>
              <span class="group-pager-info">第 {{ g.page.value }} / {{ totalPages(g.total) }} 页</span>
              <button class="btn btn-ghost btn-xs" :disabled="g.page.value >= totalPages(g.total)" @click="nextPage(g)">下一页</button>
            </div>

            <div v-if="g.docs.length" class="table-wrap">
              <table class="doc-table">
                <thead>
                  <tr>
                    <th class="col-name" style="width: 400px;">文档</th>
                    <th class="col-ver">版本</th>
                    <th class="col-chunks">片段</th>
                    <th class="col-status">状态</th>
                    <th class="col-share">共享</th>
                    <th class="col-owner">归属</th>
                    <th class="col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="d in g.docs" :key="d.id">
                    <td class="col-name">
                      <div class="file-cell">
                        <span class="file-icon" :class="d.sync_status"></span>
                        <div class="file-meta">
                          <span class="file-name" :title="d.file_name">{{ d.file_name }}</span>
                          <span class="file-source" :title="d.source">{{ d.source }}</span>
                        </div>
                      </div>
                    </td>
                    <td class="col-ver"><span class="mono">v{{ d.version }}</span></td>
                    <td class="col-chunks"><span class="mono">{{ d.chunk_count }}</span></td>
                    <td class="col-status"><StatusBadge :status="d.sync_status" /></td>
                    <td class="col-share">
                      <span v-if="d.is_public" class="share-badge public">
                        <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M8 2v8M4 6l4-4 4 4M2 12v2h12v-2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
                        共享
                      </span>
                      <span v-else class="share-badge private">私有</span>
                    </td>
                    <td class="col-owner">
                      <span v-if="isMine(d)" class="owner-mine">我</span>
                      <span v-else class="owner-other" title="来自其他用户共享">他人共享</span>
                    </td>
                    <td class="col-actions">
  <div class="row-actions">
    <button class="icon-btn" :title="isMine(d) ? '下载原件' : '下载共享原件'" @click="download(d)">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M10 3v10M6 9l4 4 4-4M4 16h12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <button v-if="g.manageable && d.sync_status !== 'pending'" class="icon-btn" title="删除" @click="remove(d)">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M5 6h10M8 3h4M7 6l.5 11h5L13 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <button v-if="g.manageable && !d.is_public" class="icon-btn share" title="设为共享" @click="share(d)">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 2v8M4 6l4-4 4 4M2 12v2h12v-2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <button v-if="g.manageable && d.is_public" class="icon-btn revoke" title="设为私有" @click="revoke(d)">
      <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M4 14a6 6 0 0 1 11-3m1 5a6 6 0 0 1-11 3m0-4 3 0m-3 0 0-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
  </div>
</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div v-if="g.docs.length" class="doc-cards">
              <div v-for="d in g.docs" :key="d.id" class="doc-card">
                <div class="card-top">
                  <span class="file-icon" :class="d.sync_status"></span>
                  <div class="file-meta">
                    <span class="file-name" :title="d.file_name">{{ d.file_name }}</span>
                    <span class="file-source" :title="d.source">{{ d.source }}</span>
                  </div>
                  <span v-if="d.is_public" class="share-badge public">共享</span>
                  <span v-else class="share-badge private">私有</span>
                </div>
                <div class="card-stats">
                  <span>v{{ d.version }}</span>
                  <span>{{ d.chunk_count }} 片段</span>
                  <StatusBadge :status="d.sync_status" />
                </div>
                <div class="card-actions">
                  <span class="owner">{{ isMine(d) ? '我' : '他人共享' }}</span>
                  <div class="row-actions">
                    <button class="btn btn-subtle" @click="download(d)">下载</button>
                    <template v-if="g.manageable && d.sync_status !== 'pending'">
                      <button class="btn btn-danger" @click="remove(d)">删除</button>
                    </template>
                    <button v-if="g.manageable && !d.is_public" class="btn btn-subtle" @click="share(d)">设为共享</button>
                    <button v-if="g.manageable && d.is_public" class="btn btn-subtle" @click="revoke(d)">设为私有</button>
                  </div>
                </div>
              </div>
            </div>

            <p v-if="!g.docs.length" class="group-empty">{{ g.emptyText }}</p>
          </section>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.docs-page {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 0 var(--page-pad);
}
.docs-inner { width: 100%; max-width: 1000px; }

.guest {
  display: flex;
  justify-content: center;
  padding: 80px 20px;
}
.guest-card {
  max-width: 420px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 34px 30px;
  text-align: center;
}
.guest-card h2 { margin: 0 0 10px; font-size: 20px; font-weight: 650; }
.guest-card p { margin: 0 0 22px; color: var(--text-2); font-size: 14px; line-height: 1.7; }

.docs-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 26px 0 6px;
}
.docs-head h1 { margin: 0; font-size: 21px; font-weight: 650; letter-spacing: -0.01em; }
.docs-head .sub { margin: 6px 0 0; color: var(--text-2); font-size: 13.5px; }
.head-actions { display: flex; align-items: center; gap: 14px; }

.share-toggle { display: inline-flex; align-items: center; gap: 9px; cursor: pointer; font-size: 13px; color: var(--text-2); user-select: none; }
.share-toggle input { position: absolute; opacity: 0; }
.switch-track { width: 38px; height: 22px; border-radius: 999px; background: var(--surface-3); border: 1px solid var(--border); position: relative; transition: background 0.18s ease, border-color 0.18s ease; }
.switch-thumb { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; box-shadow: var(--shadow-xs); transition: transform 0.18s ease; }
.share-toggle input:checked + .switch-track { background: var(--accent); border-color: var(--accent); }
.share-toggle input:checked + .switch-track .switch-thumb { transform: translateX(16px); }
.toggle-label { white-space: nowrap; }

.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }

.error-banner {
  color: var(--err);
  font-size: 13.5px;
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin: 14px 0;
  text-align: left;
}

.queue-stats-bar { display: flex; justify-content: flex-end; margin: 12px 0 2px; }
.queue-stats { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--text-3); }
.qs { display: inline-flex; align-items: center; gap: 5px; }
.qs .dot { width: 6px; height: 6px; border-radius: 50%; }
.dot.ok { background: var(--ok); }
.dot.warn { background: var(--warn); }
.dot.err { background: var(--err); }

.docs-groups { display: flex; flex-direction: column; gap: 26px; margin-top: 10px; }
.doc-group { }
.group-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 10px; }
.group-head h2 { margin: 0; font-size: 15px; font-weight: 650; letter-spacing: -0.01em; color: var(--text); }
.group-count { font-family: var(--font-mono); font-size: 11px; color: var(--text-3); background: var(--surface-3); border-radius: var(--radius-pill); padding: 2px 8px; }
.group-hint { font-size: 12px; color: var(--text-3); margin-left: auto; }
.doc-group.is-other .group-head h2 { color: var(--text-2); }
.group-empty { font-size: 13px; color: var(--text-3); padding: 22px 4px; }

.table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-xs); }
.doc-table { width: 100%; border-collapse: collapse; }
.doc-table th { text-align: left; font-size: 12px; font-weight: 500; color: var(--text-3); padding: 12px 16px; border-bottom: 1px solid var(--border); background: var(--surface-2); letter-spacing: 0.02em; }
.doc-table td { padding: 13px 16px; border-bottom: 1px solid var(--border); font-size: 13.5px; vertical-align: middle; }
.doc-table tbody tr:last-child td { border-bottom: none; }
.doc-table tbody tr:hover { background: var(--surface-2); }

.file-cell { display: flex; align-items: center; gap: 12px; }
.file-icon { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: 8px; flex: none; background: var(--neutral-soft); }
.file-icon.in_sync { background: var(--ok-soft); }
.file-icon.pending { background: var(--warn-soft); }
.file-icon.failed { background: var(--err-soft); }
.file-meta { min-width: 0; }
.file-name { display: block; font-weight: 500; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 360px; }
.file-source { display: block; font-family: var(--font-mono); font-size: 11px; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 360px; }
.mono { font-family: var(--font-mono); font-size: 12.5px; color: var(--text-2); }

.share-badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 500; padding: 3px 9px; border-radius: var(--radius-pill); }
.share-badge.public { color: var(--accent); background: var(--accent-soft); }
.share-badge.private { color: var(--text-3); background: var(--neutral-soft); }

.owner-mine { color: var(--text-2); font-size: 12.5px; }
.owner-other { color: var(--accent); font-size: 12.5px; background: var(--accent-softer); padding: 2px 8px; border-radius: var(--radius-pill); }

.row-actions { display: inline-flex; align-items: center; gap: 4px; }
.icon-btn.revoke { color: var(--warn); }
.icon-btn.share { color: var(--accent); }

.doc-cards { display: flex; flex-direction: column; gap: 10px; }
.doc-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px; box-shadow: var(--shadow-xs); }
.card-top { display: flex; align-items: center; gap: 12px; }
.card-stats { display: flex; align-items: center; gap: 12px; margin: 12px 0; color: var(--text-2); font-size: 12.5px; flex-wrap: wrap; }
.card-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; border-top: 1px solid var(--border); padding-top: 12px; }
.owner { font-size: 12px; color: var(--text-3); }

.empty { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 70px 20px; text-align: center; }
.empty-mark { width: 60px; height: 60px; border-radius: 18px; display: flex; align-items: center; justify-content: center; margin-bottom: 16px; }
.empty h3 { margin: 0 0 6px; font-size: 18px; font-weight: 650; }
.empty p { margin: 0 0 20px; color: var(--text-2); font-size: 14px; max-width: 420px; }

.queue-panel { margin: 12px 0 2px; }
.queue-toggle { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--border); background: var(--surface); border-radius: var(--radius-sm); padding: 7px 12px; color: var(--text-3); font-size: 12px; cursor: pointer; transition: border-color 0.16s ease, color 0.16s ease; }
.queue-toggle:hover { border-color: var(--border-strong); color: var(--text-2); }
.queue-toggle .chev { transition: transform 0.16s ease; color: var(--text-3); }
.queue-toggle .chev.up { transform: rotate(180deg); }
.queue-detail { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 10px; }
.queue-col { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 12px 14px; box-shadow: var(--shadow-xs); }
.queue-col h3 { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--text-2); }
.qcol-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.qcol-head h3 { margin: 0; }
.queue-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; max-height: 220px; overflow: auto; }
.queue-list li { font-size: 12.5px; padding: 6px 8px; background: var(--surface-2); border-radius: var(--radius-sm); }
.qname { font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qname-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.qmeta { font-size: 11px; color: var(--text-3); margin-left: 6px; flex: none; }
.qerr { display: block; font-size: 11px; color: var(--err); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qempty { font-size: 12px; color: var(--text-3); padding: 8px 0; }
.btn-xs { padding: 3px 8px; font-size: 11.5px; }
.icon-btn.retry { color: var(--accent); }
.drop-enter-active, .drop-leave-active { transition: opacity 0.14s ease, transform 0.14s ease; }
.drop-enter-from, .drop-leave-to { opacity: 0; transform: translateY(-4px); }

.group-pager { display: flex; align-items: center; gap: 10px; margin: -2px 0 12px; }
.group-pager-info { font-size: 12px; color: var(--text-3); }

@media (max-width: 820px) {
  .docs-head { flex-direction: column; align-items: stretch; }
  .head-actions { justify-content: space-between; }
  .queue-stats-bar { justify-content: flex-start; }
  .queue-detail { grid-template-columns: 1fr; }
  .table-wrap { display: none; }
  .doc-cards { display: flex; }
  .group-hint { margin-left: 0; }
}
@media (min-width: 821px) { .doc-cards { display: none; } }
</style>

