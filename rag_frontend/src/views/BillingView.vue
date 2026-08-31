<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  getAdminBillingOverview,
  getAdminBillingUsers,
  getBillingSummary,
  getBillingUsage,
  type AdminBillingOverview,
  type AdminUserUsage,
  type BillingBucket,
  type BillingDaily,
  type BillingRange,
  type BillingSummary,
  type UsageList,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'

const auth = useAuthStore()
const feedback = useFeedback()

const RANGES: Array<{ key: BillingRange; label: string }> = [
  { key: 'today', label: '今天' },
  { key: '7d', label: '近 7 天' },
  { key: '30d', label: '近 30 天' },
  { key: 'all', label: '全部' },
]

const TYPE_LABELS: Record<string, string> = {
  intent: '意图识别',
  answer: '知识问答',
  chat: '闲聊',
  summarize: '摘要',
}

const MODEL_LABELS: Record<string, string> = {
  'deepseek-v4-flash': 'DeepSeek',
  'qwen3.7-flash': 'Qwen',
}

const range = ref<BillingRange>('7d')
const loading = ref(false)
const error = ref('')

const summary = ref<BillingSummary | null>(null)
const usage = ref<UsageList>({ total: 0, items: [] })
const usageLoading = ref(false)
const usagePage = ref(1)
const usagePageSize = 20
const typeFilter = ref('')

const adminOverview = ref<AdminBillingOverview | null>(null)
const adminUsers = ref<AdminUserUsage[]>([])
const adminUsersTotal = ref(0)
const adminLoading = ref(false)
const adminPage = ref(1)
const adminPageSize = 20
const adminQ = ref('')

const tab = ref<'mine' | 'all'>('mine')

const typeOptions = computed(() => {
  const seen = new Set<string>()
  const list: Array<{ key: string; label: string }> = []
  for (const b of summary.value?.by_type ?? []) {
    if (!seen.has(b.key)) {
      seen.add(b.key)
      list.push({ key: b.key, label: TYPE_LABELS[b.key] ?? b.key })
    }
  }
  return list
})

function bucketLabel(b: BillingBucket, kind: 'type' | 'model'): string {
  const map = kind === 'type' ? TYPE_LABELS : MODEL_LABELS
  return map[b.key] ?? b.key
}

function fmtCost(n: number | null | undefined): string {
  const v = Number(n ?? 0)
  if (v >= 1) return `¥${v.toFixed(2)}`
  if (v === 0) return '¥0'
  return `¥${v.toFixed(6).replace(/\.?0+$/, '')}`
}

function fmtNum(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString('zh-CN')
}

function fmtDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function fmtDate(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function fmtLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${ms}ms`
}

async function loadSummary() {
  loading.value = true
  error.value = ''
  try {
    summary.value = await getBillingSummary(range.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

async function loadUsage() {
  usageLoading.value = true
  try {
    usage.value = await getBillingUsage({
      page: usagePage.value,
      page_size: usagePageSize,
      type: typeFilter.value || undefined,
    })
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  } finally {
    usageLoading.value = false
  }
}

function switchType(t: string) {
  typeFilter.value = t
  usagePage.value = 1
  void loadUsage()
}

async function loadAdmin() {
  adminLoading.value = true
  try {
    adminOverview.value = await getAdminBillingOverview(range.value)
    await loadAdminUsers()
  } catch (e) {
    feedback.notify(e instanceof Error ? e.message : String(e), 'error')
  } finally {
    adminLoading.value = false
  }
}

async function loadAdminUsers() {
  const data = await getAdminBillingUsers({
    page: adminPage.value,
    page_size: adminPageSize,
    q: adminQ.value || undefined,
  })
  adminUsers.value = data.items
  adminUsersTotal.value = data.total
}

let adminSearchTimer: ReturnType<typeof setTimeout> | undefined
function onAdminSearch() {
  clearTimeout(adminSearchTimer)
  adminSearchTimer = setTimeout(() => {
    adminPage.value = 1
    void loadAdminUsers().catch((e) =>
      feedback.notify(e instanceof Error ? e.message : String(e), 'error'),
    )
  }, 300)
}

function switchRange(r: BillingRange) {
  if (range.value === r) return
  range.value = r
  usagePage.value = 1
  void loadSummary()
  if (tab.value === 'all') void loadAdmin()
}

function switchTab(t: 'mine' | 'all') {
  if (tab.value === t) return
  tab.value = t
  if (t === 'all') void loadAdmin()
  else void loadUsage()
}

const totalPages = computed(() => Math.max(1, Math.ceil(usage.value.total / usagePageSize)))
const adminTotalPages = computed(() => Math.max(1, Math.ceil(adminUsersTotal.value / adminPageSize)))

const dailyMax = computed(() => Math.max(1, ...(summary.value?.daily ?? []).map((d) => d.cost)))
const dailyBars = computed(() => summary.value?.daily ?? [])

function barHeight(d: BillingDaily): string {
  return `${Math.max(2, Math.round((d.cost / dailyMax.value) * 100))}%`
}

function sharePct(b: BillingBucket, kind: 'type' | 'model'): number {
  const list = kind === 'type' ? summary.value?.by_type ?? [] : summary.value?.by_model ?? []
  const total = list.reduce((s, x) => s + x.cost, 0)
  if (total <= 0) return 0
  return Math.round((b.cost / total) * 100)
}

onMounted(() => {
  void loadSummary()
  void loadUsage()
})
</script><template>
  <div class="billing-page">
    <div class="billing-inner">
      <header class="bl-head">
        <div>
          <h1>用量与费用</h1>
          <p class="sub">每次 LLM 调用的 token 消耗与预估费用，按请求汇总展示。</p>
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

      <div v-if="auth.isAdmin" class="view-tabs" role="tablist" aria-label="用量视图">
        <button
          class="view-tab"
          :class="{ active: tab === 'mine' }"
          role="tab"
          :aria-selected="tab === 'mine'"
          @click="switchTab('mine')"
        >
          我的用量
        </button>
        <button
          class="view-tab"
          :class="{ active: tab === 'all' }"
          role="tab"
          :aria-selected="tab === 'all'"
          @click="switchTab('all')"
        >
          全部用户
        </button>
      </div>

      <!-- 我的用量 -->
      <template v-if="tab === 'mine'">
        <div v-if="loading" class="loading-block">加载中…</div>

        <template v-else-if="summary">
          <section class="stats-row" aria-label="用量统计">
            <div class="stat-card">
              <span class="stat-num cost">{{ fmtCost(summary.total_cost) }}</span>
              <span class="stat-label">累计费用</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(summary.request_count) }}</span>
              <span class="stat-label">请求次数</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(summary.total_requests) }}</span>
              <span class="stat-label">LLM 调用次数</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(summary.total_tokens) }}</span>
              <span class="stat-label">总 tokens</span>
            </div>
            <div class="stat-card">
              <span class="stat-num small">{{ fmtCost(summary.avg_cost) }}</span>
              <span class="stat-label">平均每次请求</span>
            </div>
          </section>

          <section class="card trend-card">
            <div class="section-head">
              <div>
                <h3 class="section-title">每日费用趋势</h3>
                <p class="section-sub">
                  输入 {{ fmtNum(summary.input_tokens) }} · 缓存
                  {{ fmtNum(summary.cached_tokens) }} · 未缓存
                  {{ fmtNum(summary.uncached_tokens) }} · 输出
                  {{ fmtNum(summary.output_tokens) }}
                </p>
              </div>
            </div>
            <div v-if="dailyBars.length" class="trend-chart" aria-label="每日费用柱状图">
              <div
                v-for="d in dailyBars"
                :key="d.date"
                class="trend-col"
                :title="`${d.date}：${fmtCost(d.cost)} / ${d.requests} 次`"
              >
                <div class="trend-bar-wrap">
                  <div class="trend-bar" :style="{ height: barHeight(d) }"></div>
                </div>
                <span class="trend-label">{{ d.date.slice(5) }}</span>
              </div>
            </div>
            <p v-else class="empty-inline">该周期内暂无调用</p>
          </section>          <section class="dist-row">
            <div class="card dist-card">
              <h3 class="section-title">按类型</h3>
              <ul v-if="summary.by_type.length" class="dist-list">
                <li v-for="b in summary.by_type" :key="b.key" class="dist-item">
                  <div class="dist-top">
                    <span class="dist-name">{{ bucketLabel(b, 'type') }}</span>
                    <span class="dist-meta">{{ b.requests }} 次 · {{ fmtCost(b.cost) }}</span>
                  </div>
                  <div class="dist-track">
                    <div class="dist-fill" :style="{ width: `${sharePct(b, 'type')}%` }"></div>
                  </div>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无数据</p>
            </div>

            <div class="card dist-card">
              <h3 class="section-title">按模型</h3>
              <ul v-if="summary.by_model.length" class="dist-list">
                <li v-for="b in summary.by_model" :key="b.key" class="dist-item">
                  <div class="dist-top">
                    <span class="dist-name">{{ bucketLabel(b, 'model') }}</span>
                    <span class="dist-meta">{{ fmtNum(b.tokens) }} tokens · {{ fmtCost(b.cost) }}</span>
                  </div>
                  <div class="dist-track">
                    <div class="dist-fill model" :style="{ width: `${sharePct(b, 'model')}%` }"></div>
                  </div>
                </li>
              </ul>
              <p v-else class="empty-inline">暂无数据</p>
            </div>
          </section>

          <section class="card usage-card">
            <div class="section-head">
              <div>
                <h3 class="section-title">最近调用</h3>
                <p class="section-sub">共 {{ fmtNum(usage.total) }} 条</p>
              </div>
              <div class="type-switch" role="tablist" aria-label="调用类型过滤">
                <button class="type-btn" :class="{ active: typeFilter === '' }" @click="switchType('')">全部</button>
                <button v-for="t in typeOptions" :key="t.key" class="type-btn" :class="{ active: typeFilter === t.key }" @click="switchType(t.key)">{{ t.label }}</button>
              </div>
            </div>

            <div v-if="usageLoading && !usage.items.length" class="loading-block">加载中…</div>

            <div v-else-if="usage.items.length" class="table-wrap">
              <table class="usage-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>类型</th>
                    <th>模型</th>
                    <th class="num">输入</th>
                    <th class="num">缓存</th>
                    <th class="num">未缓存</th>
                    <th class="num">输出</th>
                    <th class="num">总 tokens</th>
                    <th class="num">费用</th>
                    <th class="num">耗时</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="u in usage.items" :key="u.id">
                    <td class="time">{{ fmtDateTime(u.created_at) }}</td>
                    <td><span class="type-tag">{{ TYPE_LABELS[u.type] ?? u.type }}</span></td>
                    <td class="mono">{{ MODEL_LABELS[u.model] ?? u.model }}</td>
                    <td class="num">{{ fmtNum(u.input_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.cached_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.uncached_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.output_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.total_tokens) }}</td>
                    <td class="num cost-cell">{{ fmtCost(u.estimated_cost) }}</td>
                    <td class="num">{{ fmtLatency(u.latency_ms) }}</td>
                    <td><span class="status-dot" :class="u.status">{{ u.status === 'success' ? '成功' : '失败' }}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div v-else-if="!usageLoading" class="empty-inline">暂无调用记录</div>

            <div v-if="usage.total > usagePageSize" class="pager">
              <button class="btn btn-ghost" :disabled="usagePage <= 1" @click="usagePage--; loadUsage()">上一页</button>
              <span class="pager-info">第 {{ usagePage }} / {{ totalPages }} 页</span>
              <button class="btn btn-ghost" :disabled="usagePage >= totalPages" @click="usagePage++; loadUsage()">下一页</button>
            </div>
          </section>
        </template>
      </template>      <template v-else-if="tab === 'all' && auth.isAdmin">
        <div v-if="adminLoading" class="loading-block">加载中…</div>

        <template v-else-if="adminOverview">
          <section class="stats-row" aria-label="全站用量统计">
            <div class="stat-card">
              <span class="stat-num cost">{{ fmtCost(adminOverview.total_cost) }}</span>
              <span class="stat-label">全站累计费用</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(adminOverview.request_count) }}</span>
              <span class="stat-label">请求次数</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(adminOverview.total_requests) }}</span>
              <span class="stat-label">LLM 调用次数</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(adminOverview.active_users) }}</span>
              <span class="stat-label">活跃用户</span>
            </div>
            <div class="stat-card">
              <span class="stat-num">{{ fmtNum(adminOverview.total_tokens) }}</span>
              <span class="stat-label">总 tokens</span>
            </div>
          </section>

          <section class="card top-card">
            <div class="section-head">
              <div>
                <h3 class="section-title">费用排行</h3>
                <p class="section-sub">按预估费用取前 10 名</p>
              </div>
            </div>
            <ol v-if="adminOverview.top_users.length" class="top-list">
              <li v-for="(t, i) in adminOverview.top_users" :key="t.user_id" class="top-item">
                <span class="top-rank">{{ i + 1 }}</span>
                <span class="top-name">{{ t.username || `用户 ${t.user_id}` }}</span>
                <span class="top-meta">{{ t.requests }} 次 · {{ fmtNum(t.tokens) }} tokens</span>
                <span class="top-cost">{{ fmtCost(t.cost) }}</span>
              </li>
            </ol>
            <p v-else class="empty-inline">暂无用量数据</p>
          </section>

          <section class="card users-card">
            <div class="section-head">
              <div>
                <h3 class="section-title">用户用量</h3>
                <p class="section-sub">共 {{ fmtNum(adminUsersTotal) }} 个用户</p>
              </div>
              <input v-model="adminQ" class="field search-input" placeholder="按用户名搜索" @input="onAdminSearch" />
            </div>

            <div v-if="adminUsers.length" class="table-wrap">
              <table class="usage-table">
                <thead>
                  <tr>
                    <th>用户</th>
                    <th class="num">请求数</th>
                    <th class="num">调用次数</th>
                    <th class="num">输入</th>
                    <th class="num">输出</th>
                    <th class="num">总 tokens</th>
                    <th class="num">费用</th>
                    <th>最近使用</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="u in adminUsers" :key="u.user_id">
                    <td class="mono">{{ u.username || `用户 ${u.user_id}` }}</td>
                    <td class="num">{{ fmtNum(u.request_count) }}</td>
                    <td class="num">{{ fmtNum(u.total_requests) }}</td>
                    <td class="num">{{ fmtNum(u.input_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.output_tokens) }}</td>
                    <td class="num">{{ fmtNum(u.total_tokens) }}</td>
                    <td class="num cost-cell">{{ fmtCost(u.total_cost) }}</td>
                    <td class="time">{{ fmtDate(u.last_used_at) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="empty-inline">暂无用户用量</div>

            <div v-if="adminUsersTotal > adminPageSize" class="pager">
              <button class="btn btn-ghost" :disabled="adminPage <= 1" @click="adminPage--; loadAdminUsers()">上一页</button>
              <span class="pager-info">第 {{ adminPage }} / {{ adminTotalPages }} 页</span>
              <button class="btn btn-ghost" :disabled="adminPage >= adminTotalPages" @click="adminPage++; loadAdminUsers()">下一页</button>
            </div>
          </section>
        </template>
      </template>
    </div>
  </div>
</template><style scoped>
.billing-page {
  flex: 1;
  display: flex;
  justify-content: center;
  padding: 0 var(--page-pad) 40px;
}
.billing-inner {
  width: 100%;
  max-width: 1040px;
}

.bl-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 26px 0 6px;
  flex-wrap: wrap;
}
.bl-head h1 { margin: 0; font-size: 21px; font-weight: 650; letter-spacing: -0.01em; }
.bl-head .sub { margin: 6px 0 0; color: var(--text-2); font-size: 13.5px; }

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

.view-tabs {
  display: inline-flex;
  gap: 4px;
  margin: 16px 0 0;
  border-bottom: 1px solid var(--border);
  width: 100%;
}
.view-tab {
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-2);
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: color 0.14s ease, border-color 0.14s ease;
}
.view-tab:hover { color: var(--text); }
.view-tab.active { color: var(--accent); border-bottom-color: var(--accent); }

.loading-block { padding: 60px 20px; text-align: center; color: var(--text-3); font-size: 14px; }
.empty-inline { margin: 0; color: var(--text-3); font-size: 13px; padding: 12px 2px; }

.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin-top: 18px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xs);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.stat-num { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; color: var(--accent); overflow-wrap: anywhere; }
.stat-num.cost { color: var(--warn); }
.stat-num.small { font-size: 18px; }
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

.trend-chart {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 180px;
  padding-top: 12px;
}
.trend-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  height: 100%;
}
.trend-bar-wrap {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  min-height: 0;
}
.trend-bar {
  width: 70%;
  max-width: 34px;
  border-radius: 5px 5px 2px 2px;
  background: var(--accent-soft);
  border: 1px solid var(--accent);
  border-bottom: none;
  transition: background 0.14s ease;
}
.trend-col:hover .trend-bar { background: var(--accent); }
.trend-label {
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}

.dist-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.dist-card { margin-top: 18px; }
.dist-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.dist-item { display: flex; flex-direction: column; gap: 6px; }
.dist-top { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.dist-name { font-size: 13.5px; font-weight: 550; color: var(--text); }
.dist-meta { font-size: 12px; color: var(--text-3); white-space: nowrap; }
.dist-track { height: 8px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
.dist-fill { height: 100%; border-radius: 999px; background: var(--accent); }
.dist-fill.model { background: var(--ok); }

.type-switch { display: inline-flex; flex-wrap: wrap; gap: 4px; }
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
.usage-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.usage-table th {
  text-align: left;
  font-size: 12px;
  font-weight: 550;
  color: var(--text-3);
  padding: 9px 10px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.usage-table td {
  padding: 10px;
  border-bottom: 1px solid var(--border);
  color: var(--text-2);
  white-space: nowrap;
}
.usage-table tbody tr:last-child td { border-bottom: none; }
.usage-table tbody tr:hover td { background: var(--surface-2); }
.usage-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.usage-table th.num { text-align: right; }
.usage-table .time, .usage-table .mono { font-family: var(--font-mono); font-size: 12px; }
.usage-table .cost-cell { color: var(--warn); font-weight: 550; }
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

.top-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.top-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  transition: background 0.14s ease;
}
.top-item:hover { background: var(--surface-2); }
.top-rank {
  width: 22px;
  height: 22px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--neutral-soft);
  color: var(--neutral);
  font-size: 12px;
  font-weight: 600;
}
.top-item:nth-child(-n+3) .top-rank { background: var(--accent-soft); color: var(--accent); }
.top-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 550; color: var(--text); font-size: 13.5px; }
.top-meta { margin-left: auto; color: var(--text-3); font-size: 12.5px; white-space: nowrap; }
.top-cost { color: var(--warn); font-weight: 600; font-size: 13px; white-space: nowrap; }

.search-input { max-width: 220px; padding: 8px 12px; font-size: 13px; }

@media (max-width: 720px) {
  .dist-row { grid-template-columns: 1fr; gap: 0; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
  .bl-head { flex-direction: column; align-items: stretch; }
  .range-switch { align-self: flex-start; }
  .search-input { max-width: 100%; }
}
</style>