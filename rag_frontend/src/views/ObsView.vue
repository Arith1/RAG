<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getObsStorage,
  getObsSummary,
  getObsTraceDetail,
  listObsTraces,
  type ObsRange,
  type ObsStorage,
  type ObsSummary,
  type ObsTraceItem,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'

const auth = useAuthStore()
const feedback = useFeedback()

const RANGES: Array<{ key: ObsRange; label: string }> = [
  { key: '1h', label: '近 1 小时' },
  { key: '24h', label: '近 24 小时' },
  { key: '7d', label: '近 7 天' },
]

const INTENT_LABELS: Record<string, string> = {
  rag_ask: '知识问答',
  chat: '闲聊',
  other: '其他',
}

const SYNC_LABELS: Record<string, string> = {
  pending: '待同步',
  in_sync: '已同步',
  failed: '同步失败',
}

const range = ref<ObsRange>('24h')
const loading = ref(false)
const error = ref('')
const summary = ref<ObsSummary | null>(null)
const traces = ref<ObsTraceItem[]>([])
const tracesTotal = ref(0)
const tracesLoading = ref(false)
const page = ref(1)
const pageSize = 10
const statusFilter = ref('')
const storage = ref<ObsStorage | null>(null)
const selected = ref<ObsTraceItem | null>(null)
const detailLoading = ref(false)
const detailError = ref('')

const totalPages = computed(() => Math.max(1, Math.ceil(tracesTotal.value / pageSize)))

function fmtPct(r: number | null | undefined): string {
  return `${Math.round(Number(r ?? 0) * 100)}%`
}

function fmtNum(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString('zh-CN')
}

function fmtMs(ms: number | null | undefined): string {
  const v = Number(ms ?? 0)
  if (v >= 1000) return `${(v / 1000).toFixed(1)}s`
  return `${Math.round(v)}ms`
}

function fmtDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function intentLabel(i: string | null): string {
  return i ? (INTENT_LABELS[i] ?? i) : '—'
}

function syncLabel(s: string): string {
  return SYNC_LABELS[s] ?? s
}

function intentShare(bucket: { intent: string | null; count: number }): number {
  const total = (summary.value?.intent_distribution ?? []).reduce((s, x) => s + x.count, 0)
  if (total <= 0) return 0
  return Math.round((bucket.count / total) * 100)
}

function syncShare(bucket: { status: string; count: number }): number {
  const total = storage.value?.documents.total ?? 0
  if (total <= 0) return 0
  return Math.round((bucket.count / total) * 100)
}

function wfPct(ms: number | null | undefined): number {
  const total = selected.value?.total_ms ?? 0
  if (total <= 0) return 0
  return Math.min(100, Math.max(3, Math.round((Number(ms ?? 0) / total) * 100)))
}

async function loadSummary() {
  loading.value = true
  error.value = ''
  try {
    summary.value = await getObsSummary(range.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadTraces() {
  tracesLoading.value = true
  try {
    const data = await listObsTraces({
      page: page.value,
      page_size: pageSize,
      status: statusFilter.value || undefined,
    })
    traces.value = data.items
    tracesTotal.value = data.total
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  } finally {
    tracesLoading.value = false
  }
}

async function loadStorage() {
  if (!auth.isAdmin) return
  try {
    storage.value = await getObsStorage()
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  }
}

async function openDetail(item: ObsTraceItem) {
  selected.value = item
  detailError.value = ''
  detailLoading.value = true
  try {
    selected.value = await getObsTraceDetail(item.request_id)
  } catch (e) {
    detailError.value = e instanceof Error ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

function switchStatus(s: string) {
  if (statusFilter.value === s) return
  statusFilter.value = s
  page.value = 1
  void loadTraces()
}

function switchRange(r: ObsRange) {
  if (range.value === r) return
  range.value = r
  page.value = 1
  void loadSummary()
  void loadTraces()
}

onMounted(() => {
  void loadSummary()
  void loadTraces()
  void loadStorage()
})
</script>

<template>
  <div class="obs-page">
    <div class="obs-inner">
      <header class="obs-head">
        <div>
          <h1>监控</h1>
          <p class="sub">RAG 请求链路：意图识别 → 检索 → 精排 → 生成，按请求追踪各阶段指标。</p>
        </div>
        <div class="range-switch" role="tablist" aria-label="统计周期">
          <button
            v-for="r in RANGES"
            :key="r.key"
            class="range-btn"
            :class="{ active: range === r.key }"
            role="tab"
            :aria-selected="range === r.key"
            @click="switchRange(r.key)"
          >
            {{ r.label }}
          </button>
        </div>
      </header>

      <p v-if="error" class="error-banner" role="alert">{{ error }}</p>

      <template v-if="summary">
        <section class="stats-row" aria-label="链路统计">
          <div class="stat-card">
            <span class="stat-num">{{ fmtNum(summary.requests) }}</span>
            <span class="stat-label">请求量</span>
          </div>
          <div class="stat-card">
            <span class="stat-num ok">{{ fmtPct(summary.success_rate) }}</span>
            <span class="stat-label">成功率</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ fmtMs(summary.avg_total_ms) }}</span>
            <span class="stat-label">平均总耗时</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ fmtMs(summary.avg_retrieval_ms) }}</span>
            <span class="stat-label">平均检索</span>
          </div>
          <div class="stat-card">
            <span class="stat-num">{{ fmtMs(summary.avg_generation_ms) }}</span>
            <span class="stat-label">平均生成</span>
          </div>
          <div class="stat-card">
            <span class="stat-num warn">{{ fmtPct(summary.zero_hit_rate) }}</span>
            <span class="stat-label">零命中率</span>
          </div>
          <div class="stat-card">
            <span class="stat-num warn">{{ fmtPct(summary.rerank_degraded_rate) }}</span>
            <span class="stat-label">精排降级率</span>
          </div>
          <div class="stat-card">
            <span class="stat-num ok">{{ fmtPct(summary.retrieval_cache_hit_rate) }}</span>
            <span class="stat-label">检索缓存命中</span>
          </div>
        </section>

        <section class="card intent-card">
          <div class="section-head">
            <div>
              <h3 class="section-title">意图分布</h3>
              <p class="section-sub">活跃用户 {{ fmtNum(summary.active_users) }} · 平均意图识别 {{ fmtMs(summary.avg_intent_ms) }}</p>
            </div>
          </div>
          <ul v-if="summary.intent_distribution.length" class="dist-list">
            <li v-for="b in summary.intent_distribution" :key="b.intent ?? 'unknown'" class="dist-item">
              <div class="dist-top">
                <span class="dist-name">{{ intentLabel(b.intent) }}</span>
                <span class="dist-meta">{{ b.count }} 次</span>
              </div>
              <div class="dist-track">
                <div class="dist-fill" :style="{ width: `${intentShare(b)}%` }"></div>
              </div>
            </li>
          </ul>
          <p v-else class="empty-inline">该周期内暂无链路</p>
        </section>

        <section class="card intent-card">
          <div class="section-head">
            <div>
              <h3 class="section-title">Top 慢请求</h3>
              <p class="section-sub">当前周期内端到端耗时最长的 5 条</p>
            </div>
          </div>
          <ul v-if="summary.slowest && summary.slowest.length" class="dist-list">
            <li v-for="(s, i) in summary.slowest" :key="s.request_id" class="dist-item">
              <div class="dist-top">
                <span class="dist-name">{{ fmtNum(i + 1) }}. {{ s.query || '—' }} <span class="dist-meta">{{ intentLabel(s.intent) }}</span></span>
                <span class="dist-meta">{{ fmtMs(s.total_ms) }}</span>
              </div>
            </li>
          </ul>
          <p v-else class="empty-inline">该周期内暂无慢请求</p>
        </section>

        <section class="card intent-card">
          <div class="section-head">
            <div>
              <h3 class="section-title">失败分布</h3>
              <p class="section-sub">当前周期内失败请求按错误类型聚合</p>
            </div>
          </div>
          <ul v-if="summary.failure_distribution && summary.failure_distribution.length" class="dist-list">
            <li v-for="f in summary.failure_distribution" :key="f.error_type ?? 'unknown'" class="dist-item">
              <div class="dist-top">
                <span class="dist-name">{{ f.error_type || 'unknown' }}</span>
                <span class="dist-meta">{{ f.count }} 次</span>
              </div>
            </li>
          </ul>
          <p v-else class="empty-inline">当前周期无失败</p>
        </section>
      </template>

      <section class="card trace-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">请求追踪</h3>
            <p class="section-sub">共 {{ fmtNum(tracesTotal) }} 条</p>
          </div>
          <div class="status-switch" role="tablist" aria-label="状态过滤">
            <button class="type-btn" :class="{ active: statusFilter === '' }" @click="switchStatus('')">全部</button>
            <button class="type-btn" :class="{ active: statusFilter === 'success' }" @click="switchStatus('success')">成功</button>
            <button class="type-btn" :class="{ active: statusFilter === 'failed' }" @click="switchStatus('failed')">失败</button>
          </div>
        </div>

        <div v-if="tracesLoading && !traces.length" class="loading-block">加载中…</div>

        <div v-else-if="traces.length" class="table-wrap">
          <table class="trace-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>意图</th>
                <th>状态</th>
                <th>问题</th>
                <th class="num">总耗时</th>
                <th class="num">检索</th>
                <th class="num">生成</th>
                <th class="num">召回</th>
                <th class="num">精排</th>
                <th class="num">最高分</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in traces" :key="t.id">
                <td class="time">{{ fmtDateTime(t.created_at) }}</td>
                <td><span class="type-tag">{{ intentLabel(t.intent) }}</span></td>
                <td><span class="status-dot" :class="t.status">{{ t.status === 'success' ? '成功' : '失败' }}</span></td>
                <td class="query-cell" :title="t.query ?? ''">{{ t.query || '—' }}</td>
                <td class="num">{{ fmtMs(t.total_ms) }}</td>
                <td class="num">{{ fmtMs(t.retrieval_ms) }}</td>
                <td class="num">{{ fmtMs(t.generation_ms) }}</td>
                <td class="num">{{ fmtNum(t.recall_count) }}</td>
                <td class="num">{{ fmtNum(t.rerank_count) }}</td>
                <td class="num">{{ t.rerank_max_score ?? '—' }}</td>
                <td><button class="btn btn-ghost detail-btn" @click="openDetail(t)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-else-if="!tracesLoading" class="empty-inline">暂无追踪记录</div>

        <div v-if="tracesTotal > pageSize" class="pager">
          <button class="btn btn-ghost" :disabled="page <= 1" @click="page--; loadTraces()">上一页</button>
          <span class="pager-info">第 {{ page }} / {{ totalPages }} 页</span>
          <button class="btn btn-ghost" :disabled="page >= totalPages" @click="page++; loadTraces()">下一页</button>
        </div>
      </section>

      <section v-if="selected" class="card detail-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">链路详情</h3>
            <p class="section-sub mono">{{ selected.request_id }}</p>
          </div>
          <button class="btn btn-ghost" @click="selected = null">关闭</button>
        </div>

        <div v-if="detailLoading" class="loading-block">加载中…</div>
        <p v-else-if="detailError" class="error-banner" role="alert">{{ detailError }}</p>

        <template v-else>
          <div class="detail-grid">
            <div class="detail-cell">
              <span class="detail-label">意图</span>
              <span class="detail-value">{{ intentLabel(selected.intent) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">状态</span>
              <span class="detail-value"><span class="status-dot" :class="selected.status">{{ selected.status === 'success' ? '成功' : '失败' }}</span></span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">会话</span>
              <span class="detail-value mono">{{ selected.session_id || '—' }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">用户</span>
              <span class="detail-value mono">#{{ selected.user_id }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">类型</span>
              <span class="detail-value">{{ selected.trace_type || selected.intent || '—' }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">端到端</span>
              <span class="detail-value">{{ fmtMs(selected.total_ms) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">意图识别</span>
              <span class="detail-value">{{ fmtMs(selected.intent_ms) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">检索</span>
              <span class="detail-value">{{ fmtMs(selected.retrieval_ms) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">检索分跳</span>
              <span class="detail-value mono">emb {{ fmtMs(selected.embedding_ms) }} · mv {{ fmtMs(selected.milvus_ms) }} · rr {{ fmtMs(selected.rerank_ms) }} · cache {{ fmtMs(selected.cache_ms) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">生成</span>
              <span class="detail-value">{{ fmtMs(selected.generation_ms) }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">召回 / 精排</span>
              <span class="detail-value">{{ selected.recall_count }} / {{ selected.rerank_count }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">平均 / 最高分</span>
              <span class="detail-value">{{ selected.rerank_avg_score ?? '—' }} / {{ selected.rerank_max_score ?? '—' }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">回答长度</span>
              <span class="detail-value">{{ fmtNum(selected.answer_len) }} 字</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">检索缓存</span>
              <span class="detail-value">{{ selected.retrieval_cache_hit ? '命中' : '未命中' }} · {{ selected.retrieval_has_scope ? '有范围' : '无可见文档' }}</span>
            </div>
          </div>

          <div class="query-block">
            <div class="detail-cell">
              <span class="detail-label">原始输入</span>
              <span class="detail-value">{{ selected.query_raw || '—' }}</span>
            </div>
            <div class="detail-cell">
              <span class="detail-label">提炼后查询</span>
              <span class="detail-value">{{ selected.query || '—' }}</span>
            </div>
          </div>

          <div v-if="selected.total_ms > 0" class="waterfall">
            <div class="wf-row">
              <span class="wf-label">意图</span>
              <div class="wf-track"><div class="wf-seg intent" :style="{ width: wfPct(selected.intent_ms) + '%' }"></div></div>
              <span class="wf-val">{{ fmtMs(selected.intent_ms) }}</span>
            </div>
            <div class="wf-row">
              <span class="wf-label">检索</span>
              <div class="wf-track"><div class="wf-seg retrieval" :style="{ width: wfPct(selected.retrieval_ms) + '%' }"></div></div>
              <span class="wf-val">{{ fmtMs(selected.retrieval_ms) }}</span>
            </div>
            <div class="wf-row">
              <span class="wf-label">精排</span>
              <div class="wf-track"><div class="wf-seg rerank" :style="{ width: wfPct(selected.rerank_ms) + '%' }"></div></div>
              <span class="wf-val">{{ fmtMs(selected.rerank_ms) }}</span>
            </div>
            <div class="wf-row">
              <span class="wf-label">生成</span>
              <div class="wf-track"><div class="wf-seg generation" :style="{ width: wfPct(selected.generation_ms) + '%' }"></div></div>
              <span class="wf-val">{{ fmtMs(selected.generation_ms) }}</span>
            </div>
            <p class="wf-note">总耗时 {{ fmtMs(selected.total_ms) }} · 检索内：embedding {{ fmtMs(selected.embedding_ms) }} / Milvus {{ fmtMs(selected.milvus_ms) }} / rerank {{ fmtMs(selected.rerank_ms) }} / cache {{ fmtMs(selected.cache_ms) }}</p>
          </div>

          <p v-if="selected.error_type" class="detail-error">
            <span class="detail-label">错误</span>
            {{ selected.error_type }}：{{ selected.error_message || '—' }}
          </p>

          <div v-if="selected.sources && selected.sources.length" class="sources-block">
            <h4 class="sources-title">来源列表</h4>
            <ul class="sources-list">
              <li v-for="(s, i) in selected.sources" :key="i" class="source-item">
                <span class="source-idx">{{ i + 1 }}</span>
                <span class="source-name mono">{{ s.source || '—' }}</span>
                <span class="source-score">{{ s.score ?? '—' }}</span>
              </li>
            </ul>
          </div>
        </template>
      </section>

      <section v-if="auth.isAdmin && storage" class="card storage-card">
        <div class="section-head">
          <div>
            <h3 class="section-title">存储概览</h3>
            <p class="section-sub">文档 {{ fmtNum(storage.documents.total) }} 个 · Milvus {{ storage.milvus.row_count != null ? fmtNum(storage.milvus.row_count) : '—' }} 行 · 检索缓存 {{ storage.cache.total }} 次判定</p>
          </div>
        </div>

        <div class="storage-grid">
          <div class="storage-cell">
            <h4 class="sources-title">文档同步状态</h4>
            <ul v-if="storage.documents.by_sync_status.length" class="dist-list">
              <li v-for="b in storage.documents.by_sync_status" :key="b.status" class="dist-item">
                <div class="dist-top">
                  <span class="dist-name">{{ syncLabel(b.status) }}</span>
                  <span class="dist-meta">{{ b.count }} 个</span>
                </div>
                <div class="dist-track">
                  <div class="dist-fill sync" :class="b.status" :style="{ width: `${syncShare(b)}%` }"></div>
                </div>
              </li>
            </ul>
            <p v-else class="empty-inline">暂无文档</p>
          </div>
          <div class="storage-cell">
            <h4 class="sources-title">检索缓存命中（本进程实时）</h4>
            <div class="cache-stat">
              <span class="cache-num">{{ fmtPct(storage.cache.rate) }}</span>
              <span class="cache-meta">{{ fmtNum(storage.cache.hits) }} 次命中 / {{ fmtNum(storage.cache.total) }} 次判定</span>
            </div>
            <div class="dist-track">
              <div class="dist-fill cache" :style="{ width: `${Math.round(Number(storage.cache.rate) * 100)}%` }"></div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.obs-page {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 0 var(--page-pad) 40px;
}
.obs-inner {
  width: 100%;
  max-width: 1080px;
}

.obs-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 26px 0 6px;
  flex-wrap: wrap;
}
.obs-head h1 { margin: 0; font-size: 21px; font-weight: 650; letter-spacing: -0.01em; }
.obs-head .sub { margin: 6px 0 0; color: var(--text-2); font-size: 13.5px; }

.range-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  background: var(--surface-3);
  border-radius: var(--radius-sm);
  flex: none;
}
.range-btn {
  padding: 7px 14px;
  border-radius: var(--radius-xs);
  font-size: 13px;
  color: var(--text-2);
  transition: background 0.14s ease, color 0.14s ease;
}
.range-btn:hover { color: var(--text); }
.range-btn.active { background: var(--surface); color: var(--accent); font-weight: 550; box-shadow: var(--shadow-xs); }

.error-banner {
  color: var(--err);
  font-size: 13.5px;
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin: 14px 0;
  text-align: left;
}
.loading-block { padding: 60px 20px; text-align: center; color: var(--text-3); font-size: 14px; }
.empty-inline { margin: 0; color: var(--text-3); font-size: 13px; padding: 12px 2px; }

.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 14px;
  margin-top: 18px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.stat-num { font-size: 23px; font-weight: 700; letter-spacing: -0.02em; color: var(--accent); overflow-wrap: anywhere; }
.stat-num.ok { color: var(--ok); }
.stat-num.warn { color: var(--warn); }
.stat-label { font-size: 12.5px; color: var(--text-2); }

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 20px 22px;
  margin-top: 18px;
}
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.section-title { margin: 0; font-size: 15.5px; font-weight: 650; }
.section-sub { margin: 4px 0 0; color: var(--text-3); font-size: 12.5px; }
.mono { font-family: var(--font-mono); }

.dist-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.dist-item { display: flex; flex-direction: column; gap: 6px; }
.dist-top { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.dist-name { font-size: 13.5px; font-weight: 550; color: var(--text); }
.dist-meta { font-size: 12px; color: var(--text-3); white-space: nowrap; }
.dist-track { height: 8px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
.dist-fill { height: 100%; border-radius: 999px; background: var(--accent); }

.status-switch { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.type-btn {
  padding: 5px 11px;
  border-radius: var(--radius-pill);
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--surface-3);
  transition: background 0.14s ease, color 0.14s ease;
}
.type-btn:hover { color: var(--text); }
.type-btn.active { background: var(--accent-soft); color: var(--accent); font-weight: 550; }

.table-wrap { overflow-x: auto; }
.trace-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.trace-table th {
  text-align: left;
  font-size: 12px;
  font-weight: 550;
  color: var(--text-3);
  padding: 9px 10px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.trace-table td {
  padding: 10px;
  border-bottom: 1px solid var(--border);
  color: var(--text-2);
  white-space: nowrap;
}
.trace-table tbody tr:last-child td { border-bottom: none; }
.trace-table tbody tr:hover td { background: var(--surface-2); }
.trace-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.trace-table th.num { text-align: right; }
.trace-table .time, .trace-table .mono { font-family: var(--font-mono); font-size: 12px; }
.query-cell { max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
.detail-btn { padding: 6px 10px; }

.type-tag {
  font-size: 12px;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: var(--radius-pill);
  padding: 3px 9px;
  white-space: nowrap;
}
.status-dot { font-size: 12px; }
.status-dot.success { color: var(--ok); }
.status-dot.failed { color: var(--err); }

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 14px 0 2px;
}
.pager .btn { min-width: 76px; justify-content: center; }
.pager-info { font-size: 12.5px; color: var(--text-3); white-space: nowrap; }

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.detail-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  min-width: 0;
}
.detail-label { font-size: 11.5px; color: var(--text-3); }
.detail-value { font-size: 13.5px; font-weight: 550; color: var(--text); overflow-wrap: anywhere; }
.detail-error {
  margin: 14px 0 0;
  font-size: 13px;
  color: var(--err);
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
}

.sources-block { margin-top: 16px; }
.query-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 12px;
  padding: 10px 14px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.waterfall { margin-top: 14px; }
.wf-row { display: flex; align-items: center; gap: 10px; margin: 4px 0; }
.wf-label { flex: none; width: 34px; font-size: 12px; color: var(--text-3); }
.wf-track { flex: 1; height: 12px; background: var(--surface-2); border-radius: 6px; overflow: hidden; }
.wf-seg { height: 100%; border-radius: 6px; }
.wf-seg.intent { background: var(--accent); }
.wf-seg.retrieval { background: #4caf50; }
.wf-seg.rerank { background: #ff9800; }
.wf-seg.generation { background: #9c27b0; }
.wf-val { flex: none; width: 70px; text-align: right; font-size: 12px; color: var(--text-2); font-variant-numeric: tabular-nums; }
.wf-note { margin: 8px 0 0; font-size: 12px; color: var(--text-3); }
.sources-title { margin: 0 0 10px; font-size: 13.5px; font-weight: 650; }
.sources-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.source-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.source-idx {
  flex: none;
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
}
.source-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12.5px; }
.source-score { color: var(--text-3); font-size: 12px; white-space: nowrap; }

.storage-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.storage-cell { min-width: 0; }
.cache-stat { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.cache-num { font-size: 26px; font-weight: 700; color: var(--ok); letter-spacing: -0.02em; }
.cache-meta { font-size: 12.5px; color: var(--text-3); }
.dist-fill.sync.in_sync { background: var(--ok); }
.dist-fill.sync.pending { background: var(--warn); }
.dist-fill.sync.failed { background: var(--err); }
.dist-fill.cache { background: var(--ok); }

@media (max-width: 720px) {
  .storage-grid { grid-template-columns: 1fr; gap: 0; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
  .obs-head { flex-direction: column; align-items: stretch; }
  .range-switch { align-self: flex-start; }
}
</style>
