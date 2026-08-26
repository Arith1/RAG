<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  streamChat,
  listChatSessions,
  getChatSession,
  deleteChatSession,
  renameChatSession,
  type SourceItem,
  type ChatSessionInfo,
} from '../api/client'
import { useAuthStore } from '../stores/auth'
import SourceStrip from '../components/SourceStrip.vue'

interface MessageItem {
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
  intent?: string
  error?: boolean
}

const auth = useAuthStore()
const messages = ref<MessageItem[]>([])
const input = ref('')
const sessionId = ref<string | null>(null)
const streaming = ref(false)
const waitStart = ref(0)
const waitSeconds = ref(0)
const waitPhase = ref<'retrieving' | 'generating'>('retrieving')
let waitTimer: number | undefined
const box = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)

const isLoggedIn = computed(() => auth.isLoggedIn)

const suggestions = [
  '我上传的文档里有哪些关键结论？',
  '把文档里的要点整理成一份清单',
  '别人共享给我的资料讲了什么？',
  '复杂问题可以拆成子问题来回答吗？',
]

const shortSession = computed(() => (sessionId.value ? sessionId.value.slice(0, 8) : ''))
const currentTitle = computed(
  () => sessions.value.find((s) => s.session_id === sessionId.value)?.title ?? '新会话',
)

/* ── 侧边栏（DeepSeek 风格，可折叠） ───────────────────── */
const sessions = ref<ChatSessionInfo[]>([])
const sidebarOpen = ref(typeof window === 'undefined' ? true : window.innerWidth > 768)
const isMobile = ref(false)
const loadingSessions = ref(false)
const editingId = ref<string | null>(null)
const editingTitle = ref('')
const confirmDeleteId = ref<string | null>(null)

function onResize() {
  isMobile.value = window.innerWidth <= 768
}

function formatTime(iso: string | null): string {
  if (!iso) return ''
  const t = new Date(iso.replace(' ', 'T'))
  if (Number.isNaN(t.getTime())) return iso
  const diff = Date.now() - t.getTime()
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小时前`
  const day = Math.floor(hr / 24)
  if (day < 7) return `${day} 天前`
  const y = t.getFullYear()
  const m = String(t.getMonth() + 1).padStart(2, '0')
  const d = String(t.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

async function refreshSessions() {
  if (!isLoggedIn.value) return
  loadingSessions.value = true
  try {
    sessions.value = await listChatSessions()
  } catch {
    // 静默失败：保留旧列表，不打断问答
  } finally {
    loadingSessions.value = false
  }
}

function newConversation() {
  if (streaming.value) return
  sessionId.value = null
  messages.value = []
  input.value = ''
  confirmDeleteId.value = null
  nextTick(() => inputEl.value?.focus())
}

async function openSession(id: string) {
  if (streaming.value || id === sessionId.value) return
  sessionId.value = id
  messages.value = []
  input.value = ''
  scrollToBottom()
  try {
    const detail = await getChatSession(id)
    messages.value = detail.messages.map((m) => ({
      role: m.role,
      content: m.content,
      sources: m.sources ?? [],
      intent: m.intent ?? undefined,
      error: false,
    }))
  } catch {
    messages.value = []
  }
  scrollToBottom()
}

function startRename(s: ChatSessionInfo) {
  editingId.value = s.session_id
  editingTitle.value = s.title
  confirmDeleteId.value = null
}
function renameInputRef(el: unknown) {
  if (el instanceof HTMLInputElement) el.focus()
}
function cancelRename() {
  editingId.value = null
}
async function commitRename(s: ChatSessionInfo) {
  if (editingId.value !== s.session_id) return
  const title = editingTitle.value.trim()
  editingId.value = null
  if (!title || title === s.title) return
  try {
    await renameChatSession(s.session_id, title)
    s.title = title
  } catch {
    // 静默失败
  }
}

function askDelete(s: ChatSessionInfo) {
  confirmDeleteId.value = s.session_id
  window.setTimeout(() => {
    if (confirmDeleteId.value === s.session_id) confirmDeleteId.value = null
  }, 3000)
}
async function removeSession(s: ChatSessionInfo, confirmed = false) {
  if (!confirmed) {
    askDelete(s)
    return
  }
  confirmDeleteId.value = null
  try {
    await deleteChatSession(s.session_id)
    if (sessionId.value === s.session_id) {
      sessionId.value = null
      messages.value = []
    }
    sessions.value = sessions.value.filter((x) => x.session_id !== s.session_id)
  } catch {
    // 静默失败
  }
}

/* ── 问答主流程 ───────────────────────────────────────── */
function scrollToBottom() {
  nextTick(() => {
    const el = box.value
    if (!el) return
    el.scrollTop = el.scrollHeight
    // 等一帧：图片 / 来源卡片等异步内容渲染完成后再定位一次，确保滚到底部
    requestAnimationFrame(() => {
      const el2 = box.value
      if (el2) el2.scrollTop = el2.scrollHeight
    })
  })
}

function startWaitTimer() {
  waitStart.value = Date.now()
  waitSeconds.value = 0
  waitPhase.value = 'retrieving'
  if (waitTimer !== undefined) window.clearInterval(waitTimer)
  waitTimer = window.setInterval(() => {
    waitSeconds.value = (Date.now() - waitStart.value) / 1000
  }, 100)
}

function stopWaitTimer() {
  if (waitTimer !== undefined) {
    window.clearInterval(waitTimer)
    waitTimer = undefined
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value || !isLoggedIn.value) return
  input.value = ''
  autoGrow()
  messages.value.push({ role: 'user', content: text, sources: [] })
  const assistant = reactive<MessageItem>({ role: 'assistant', content: '', sources: [] })
  messages.value.push(assistant)
  streaming.value = true
  startWaitTimer()
  scrollToBottom()

  try {
    await streamChat({ content: text, session_id: sessionId.value }, (evt) => {
      if (evt.type === 'meta') {
        if (evt.session_id) sessionId.value = evt.session_id
        assistant.sources = evt.sources
        assistant.intent = evt.intent
        waitPhase.value = 'generating'
        scrollToBottom()
      } else if (evt.type === 'token') {
        assistant.content += evt.text
        scrollToBottom()
      } else if (evt.type === 'answer') {
        // 闲聊/提示类回复只发 answer 事件（带 session_id），不发 meta，需同步更新会话 id 以便续接
        if (evt.session_id) sessionId.value = evt.session_id
        assistant.content = evt.answer
        assistant.sources = evt.sources
        assistant.intent = evt.intent
      } else if (evt.type === 'error') {
        assistant.content = evt.message
        assistant.error = true
      }
    })
  } catch (e) {
    assistant.content = e instanceof Error ? e.message : String(e)
    assistant.error = true
  } finally {
    streaming.value = false
    stopWaitTimer()
    await refreshSessions()
    scrollToBottom()
  }
}

function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

watch(isLoggedIn, (v) => {
  if (v) {
    refreshSessions()
  } else {
    sessions.value = []
    sessionId.value = null
    messages.value = []
  }
})

onMounted(() => {
  onResize()
  window.addEventListener('resize', onResize)
  if (isLoggedIn.value) refreshSessions()
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  stopWaitTimer()
})
</script>

<template>
  <div class="chat-layout">
    <!-- 历史会话侧边栏（仅登录用户） -->
    <aside v-if="isLoggedIn" class="sidebar" :class="{ open: sidebarOpen, 'is-mobile': isMobile }">
      <template v-if="sidebarOpen">
        <div class="sidebar-head">
          <button class="new-chat-btn" :disabled="streaming" @click="newConversation">
            <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
              <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            新建会话
          </button>
          <button class="icon-btn" title="收起侧边栏" @click="sidebarOpen = false">
            <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
              <path d="M10 4L6 8l4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>

        <div class="sidebar-list scroll-thin">
          <div v-if="loadingSessions && !sessions.length" class="sidebar-tip">加载中…</div>
          <div v-else-if="!sessions.length" class="sidebar-tip">暂无历史会话，开始提问吧</div>

          <div
            v-for="s in sessions"
            :key="s.session_id"
            class="session-item"
            :class="{ active: s.session_id === sessionId }"
            @click="openSession(s.session_id)"
          >
            <span class="session-mark" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14">
                <path d="M3 2.5h10v8H7.2L4 13V10.5H3v-8Zm2 3h6M5 8h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <div class="session-body">
              <template v-if="editingId === s.session_id">
                <input
                  v-model="editingTitle"
                  class="rename-input"
                  maxlength="128"
                  placeholder="会话标题"
                  @click.stop
                  @keydown.enter="commitRename(s)"
                  @keydown.esc="cancelRename()"
                  @blur="commitRename(s)"
                  :ref="editingId === s.session_id ? renameInputRef : undefined"
                />
              </template>
              <template v-else>
                <span class="session-title">{{ s.title }}</span>
                <span class="session-preview">{{ s.last_message_preview || '…' }}</span>
                <span class="session-time">{{ formatTime(s.last_message_at) }}</span>
              </template>
            </div>
            <div class="session-actions" @click.stop>
              <template v-if="confirmDeleteId === s.session_id">
                <button class="mini-btn danger" title="确认删除" @click="removeSession(s, true)">确认</button>
                <button class="mini-btn" title="取消" @click="confirmDeleteId = null">取消</button>
              </template>
              <template v-else>
                <button class="icon-btn" title="重命名" @click="startRename(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path d="M11.3 2.7a1.6 1.6 0 0 1 2.3 0l-.4-.4a1.6 1.6 0 0 1 0 2.3L5.5 12.3 2 13.3l1-3.5 8.3-7.1Z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除" @click="askDelete(s)">
                  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                    <path d="M3 4.5h10M6.5 4.5V3.2h3v1.3M5 4.5l.5 8.3h5l.5-8.3M6.7 7v3.2M9.3 7v3.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </template>
            </div>
          </div>
        </div>

        <div class="sidebar-foot">
          <span>会话默认保留 1 天</span>
        </div>
      </template>

      <template v-else>
        <button class="rail-btn" title="展开侧边栏" @click="sidebarOpen = true">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button class="rail-btn" title="新建会话" :disabled="streaming" @click="newConversation">
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </template>
    </aside>

    <div v-if="isMobile && isLoggedIn && sidebarOpen" class="sidebar-backdrop" @click="sidebarOpen = false"></div>

    <!-- 主问答区 -->
    <div class="chat-main">
      <header class="chat-head">
        <div class="chat-title">
          <button v-if="isLoggedIn" class="icon-btn head-toggle" :title="sidebarOpen ? '收起侧边栏' : '展开侧边栏'" @click="sidebarOpen = !sidebarOpen">
            <svg v-if="!sidebarOpen" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M2 4h12M2 8h12M2 12h12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            <svg v-else viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          <span class="pulse" aria-hidden="true"></span>
          <h1>知识库问答</h1>
          <span v-if="isLoggedIn && sessionId" class="session-chip" :title="currentTitle">{{ currentTitle }}</span>
        </div>
        <div v-if="isLoggedIn" class="chat-actions">
          <button class="btn btn-subtle" @click="newConversation">新会话</button>
        </div>
      </header>

      <div class="chat-inner">
        <div ref="box" class="messages scroll-thin">
          <!-- 空状态 / 访客态 -->
          <div v-if="!messages.length" class="empty">
            <div class="empty-hero">
              <div class="empty-mark">
                <svg viewBox="0 0 64 64" width="46" height="46" aria-hidden="true">
                  <rect width="64" height="64" rx="16" fill="var(--accent)" />
                  <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="#fff" stroke-width="3.4" stroke-linejoin="round" />
                  <path d="M32 15v30" stroke="#fff" stroke-width="3.4" stroke-linecap="round" />
                  <path d="M38 7l1.1 3.1L42 11l-2.9.9L38 15l-1.1-3.1L34 11l2.9-.9L38 7Z" fill="#bcd0ff" />
                </svg>
              </div>
              <h2>{{ isLoggedIn ? '今天想问点什么？' : '登录后即可向知识库提问' }}</h2>
              <p class="empty-sub">
                答案会同时检索<b class="hl">你自己的文档</b>与<b class="hl">他人共享的文档</b>，
                并给出可追溯的来源引用。
              </p>
            </div>

            <div v-if="isLoggedIn" class="suggestions">
              <button
                v-for="s in suggestions"
                :key="s"
                class="suggestion"
                @click="input = s; autoGrow()"
              >
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M3 8h10M8 3v10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                </svg>
                {{ s }}
              </button>
            </div>

            <p v-if="!isLoggedIn" class="guest-note">
              登录后可管理自己的文档，并把它们共享给其他用户检索。
              <RouterLink to="/login" class="login-link">去登录 →</RouterLink>
            </p>
            <p v-else class="feature-note">复杂问题会被自动拆成最多 5 个子问题分别检索，再汇总成一份带引用的回答。</p>
          </div>

          <!-- 对话记录 -->
          <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
            <div v-if="m.role === 'user'" class="user-bubble">{{ m.content }}</div>

            <div v-else class="assistant">
              <div v-if="m.sources.length" class="meta-line">
                <span class="meta-intent">{{ m.intent === 'chat' ? '闲聊' : m.intent === 'other' ? '提示' : '知识库检索' }}</span>
                <span v-if="m.sources.some((s) => s.question)" class="meta-split">
                  已拆分为 {{ m.sources.filter((s) => s.question).length + 1 }} 个子问题分别检索
                </span>
                <span v-else class="meta-count">检索到 {{ m.sources.length }} 条来源</span>
              </div>

              <div class="answer" :class="{ error: m.error }">
                <template v-if="m.content">{{ m.content }}<span v-if="streaming && i === messages.length - 1" class="caret" aria-hidden="true">▍</span></template>
                <div v-else-if="streaming && i === messages.length - 1" class="waiting">
                  <span class="waiting-dot" aria-hidden="true"></span>
                  <span class="waiting-text">{{ waitPhase === 'generating' ? '正在生成回答' : '正在检索知识库' }}</span>
                  <span class="waiting-time">{{ waitSeconds.toFixed(1) }}s</span>
                </div>
              </div>

              <SourceStrip v-if="m.sources.length" :sources="m.sources" />
            </div>
          </div>

        </div>

        <!-- 输入 / 登录门 -->
        <div class="composer-wrap">
          <div v-if="isLoggedIn" class="composer">
            <textarea
              ref="inputEl"
              v-model="input"
              class="composer-input scroll-thin"
              rows="1"
              placeholder="输入问题，例如：“我从共享文档里看到了什么？”"
              :disabled="streaming"
              @input="autoGrow"
              @keydown="onKeydown"
            ></textarea>
            <button
              class="send"
              :disabled="streaming || !input.trim()"
              aria-label="发送"
              @click="send"
            >
              <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
                <path d="M3 10l13-6-4.5 13L9 12l-6-2Z" fill="currentColor" />
              </svg>
            </button>
          </div>

          <div v-else class="login-gate">
            <span class="gate-text">登录后即可开始提问和上传文档</span>
            <RouterLink to="/login" class="btn btn-primary gap">登录 / 注册</RouterLink>
          </div>

          <p class="composer-hint">
            支持多轮对话与追问指代 · Enter 发送 · Shift+Enter 换行
            <span class="hint-sep">·</span> 答案来源可追溯
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── 布局：侧边栏 + 主问答区 ─────────────────────────── */
.chat-layout {
  flex: 1;
  display: flex;
  min-height: 0;
  align-items: stretch;
}

.sidebar {
  flex: none;
  width: 52px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 12px 0;
  background: var(--bg-soft);
  border-right: 1px solid var(--border);
  transition: width 0.22s ease;
  overflow: hidden;
}
.sidebar.open {
  width: 280px;
  align-items: stretch;
  padding: 12px 10px;
  gap: 0;
}

.sidebar-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.new-chat-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  font-weight: 550;
  font-size: 13.5px;
  transition: border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}
.new-chat-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: var(--shadow-xs);
}
.new-chat-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: 8px;
  color: var(--text-3);
  transition: background 0.14s ease, color 0.14s ease;
}
.icon-btn:hover { background: var(--surface-3); color: var(--text); }
.icon-btn.danger:hover { background: var(--err-soft); color: var(--err); }

.rail-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  transition: background 0.14s ease, color 0.14s ease;
}
.rail-btn:hover { background: var(--surface-3); color: var(--text); }
.rail-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.sidebar-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-bottom: 6px;
}
.sidebar-tip {
  padding: 22px 12px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
}

.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  cursor: pointer;
  text-align: left;
  transition: background 0.14s ease;
}
.session-item:hover { background: var(--surface-3); }
.session-item.active { background: var(--accent-soft); }
.session-mark {
  flex: none;
  display: inline-flex;
  color: var(--text-3);
}
.session-item.active .session-mark { color: var(--accent); }
.session-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.session-title {
  font-size: 13.5px;
  font-weight: 550;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-preview {
  font-size: 12px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-time { font-size: 11px; color: var(--text-3); }
.session-actions {
  flex: none;
  display: none;
  align-items: center;
  gap: 2px;
}
.session-item:hover .session-actions,
.session-item.active .session-actions { display: inline-flex; }

.rename-input {
  width: 100%;
  height: 26px;
  border: 1px solid var(--accent);
  border-radius: 7px;
  padding: 0 8px;
  font-size: 13px;
  background: var(--surface);
  color: var(--text);
}
.rename-input:focus { box-shadow: var(--shadow-focus); }

.mini-btn {
  height: 24px;
  padding: 0 8px;
  border-radius: 7px;
  font-size: 12px;
  color: var(--text-2);
  background: var(--surface-3);
}
.mini-btn.danger { color: var(--err); background: var(--err-soft); }
.mini-btn:hover { filter: brightness(0.97); }

.sidebar-foot {
  padding: 10px 4px 2px;
  margin-top: 8px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-3);
  text-align: center;
}

.sidebar-backdrop {
  position: fixed;
  inset: var(--nav-h) 0 0 0;
  background: rgba(15, 23, 42, 0.28);
  z-index: 29;
}

/* ── 主问答区 ───────────────────────────────────────── */
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0 var(--page-pad);
}
.chat-inner {
  width: 100%;
  max-width: var(--content-max);
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

.chat-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  max-width: var(--content-max);
  padding: 22px 0 8px;
}
.chat-title {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.head-toggle { margin-left: -8px; }
.chat-title h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 650;
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.pulse {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
.session-chip {
  max-width: 260px;
  font-size: 12px;
  color: var(--text-3);
  background: var(--surface-3);
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-actions { display: flex; align-items: center; gap: 10px; }

.messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 4px 28px;
  scroll-behavior: smooth;
}

/* Empty / guest */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: calc(100% - 20px);
  text-align: center;
  padding: 40px 12px;
}
.empty-hero { max-width: 520px; }
.empty-mark {
  display: inline-flex;
  width: 64px;
  height: 64px;
  border-radius: 18px;
  background: var(--accent-soft);
  align-items: center;
  justify-content: center;
  margin-bottom: 20px;
}
.empty-mark svg { border-radius: 12px; }
.empty-hero h2 {
  margin: 0 0 10px;
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.015em;
}
.empty-sub {
  color: var(--text-2);
  font-size: 14.5px;
  line-height: 1.7;
  margin: 0;
}
.hl { color: var(--text); font-weight: 600; }

.suggestions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 10px;
  margin-top: 30px;
  max-width: 560px;
}
.suggestion {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 9px 14px;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--radius-pill);
  color: var(--text-2);
  font-size: 13px;
  transition: border-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
}
.suggestion svg { color: var(--accent); }
.suggestion:hover {
  border-color: var(--accent);
  color: var(--text);
  box-shadow: var(--shadow-xs);
}
.feature-note {
  margin-top: 30px;
  font-size: 12.5px;
  color: var(--text-3);
}
.guest-note {
  margin-top: 26px;
  font-size: 13px;
  color: var(--text-2);
}
.login-link {
  font-weight: 600;
  margin-left: 4px;
}

/* Messages */
.msg-row {
  display: flex;
  margin: 18px 0;
}
.msg-row.user { justify-content: flex-end; }
.user-bubble {
  max-width: 78%;
  background: var(--accent);
  color: var(--text-on-accent);
  padding: 11px 16px;
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-xs) var(--radius-lg);
  font-size: 15px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: var(--shadow-xs);
}
.msg-row.assistant { justify-content: flex-start; }
.assistant {
  width: 100%;
  max-width: 100%;
}
.meta-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 8px;
}
.meta-intent {
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  font-weight: 500;
}
.meta-split, .meta-count { color: var(--text-3); }
.answer {
  font-size: 15.5px;
  line-height: 1.8;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.answer.error { color: var(--err); }
.caret {
  display: inline-block;
  width: 8px;
  color: var(--accent);
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink { 50% { opacity: 0; } }

/* 等待检索 / 生成时的状态提示（DeepSeek 风格） */
.waiting {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--text-2);
  font-size: 14px;
  line-height: 1.6;
  min-height: 24px;
}
.waiting-dot {
  width: 8px;
  height: 8px;
  flex: none;
  border-radius: 50%;
  background: var(--accent);
  animation: waitingPulse 1.2s ease-in-out infinite;
}
@keyframes waitingPulse {
  0%, 100% { opacity: 0.25; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.12); }
}
.waiting-text { font-weight: 500; }
.waiting-time {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  font-variant-numeric: tabular-nums;
}

/* Composer */
.composer-wrap { padding: 10px 0 18px; }
.composer {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 8px 8px 8px 16px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
.composer:focus-within {
  border-color: var(--accent);
  box-shadow: var(--shadow-focus), var(--shadow-sm);
}
.composer-input {
  flex: 1;
  border: none;
  background: transparent;
  resize: none;
  font-size: 15px;
  line-height: 1.6;
  padding: 6px 0;
  max-height: 200px;
  outline: none;
  color: var(--text);
}
.composer-input::placeholder { color: var(--text-3); }
.composer-input:disabled { opacity: 0.6; }
.send {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--accent);
  color: #fff;
  transition: background 0.16s ease, opacity 0.16s ease;
}
.send:hover:not(:disabled) { background: var(--accent-strong); }
.send:disabled { background: var(--surface-3); color: var(--text-3); cursor: not-allowed; }
.composer-hint {
  margin: 8px 4px 0;
  font-size: 12px;
  color: var(--text-3);
  text-align: center;
}
.hint-sep { margin: 0 4px; }

.login-gate {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-xl);
  padding: 16px 18px;
  box-shadow: var(--shadow-sm);
}
.gate-text { color: var(--text-2); font-size: 14px; }
.gap { gap: 6px; }

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: var(--nav-h);
    bottom: 0;
    z-index: 30;
    width: 0;
    box-shadow: var(--shadow-md);
  }
  .sidebar.open { width: min(280px, 84vw); }
  .chat-main { padding: 0 14px; }
  .user-bubble { max-width: 92%; }
  .suggestions { flex-direction: column; align-items: stretch; }
  .suggestion { justify-content: flex-start; }
  .login-gate { flex-direction: column; text-align: center; }
}
</style>
