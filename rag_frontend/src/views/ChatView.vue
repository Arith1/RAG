<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { streamChat, type SourceItem } from '../api/client'

interface MessageItem {
  role: 'user' | 'assistant'
  content: string
  sources: SourceItem[]
}

const messages = ref<MessageItem[]>([])
const input = ref('')
const sessionId = ref<string | null>(null)
const streaming = ref(false)
const msgBox = ref<HTMLElement | null>(null)

function basename(path: string | null): string {
  return String(path ?? '').split('/').pop() ?? ''
}

function scrollToBottom() {
  nextTick(() => {
    msgBox.value?.scrollTo({ top: msgBox.value.scrollHeight })
  })
}

function newConversation() {
  sessionId.value = null
  messages.value = []
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text, sources: [] })
  const assistant: MessageItem = { role: 'assistant', content: '', sources: [] }
  messages.value.push(assistant)
  streaming.value = true
  scrollToBottom()

  try {
    await streamChat({ content: text, session_id: sessionId.value }, (evt) => {
      if (evt.type === 'meta') {
        assistant.sources = evt.sources
        scrollToBottom()
      } else if (evt.type === 'token') {
        assistant.content += evt.text
        scrollToBottom()
      } else if (evt.type === 'answer') {
        assistant.content = evt.answer
        assistant.sources = evt.sources
      } else if (evt.type === 'error') {
        assistant.content = '⚠️ ' + evt.message
      }
      // done：完整答案已在 token 中累积
    })
  } catch (e) {
    assistant.content = '请求失败：' + (e instanceof Error ? e.message : String(e))
  } finally {
    streaming.value = false
    scrollToBottom()
  }
}
</script>

<template>
  <div class="chat">
    <div class="head">
      <h2>知识库问答</h2>
      <button class="new" @click="newConversation">＋ 新会话</button>
    </div>

    <div class="messages" ref="msgBox">
      <div v-if="!messages.length" class="hint">
        登录后即可提问，例如：为什么用 Milvus 而不是 FAISS？<br />
        支持多轮对话（同会话记忆）与追问指代。
      </div>
      <div v-for="(m, i) in messages" :key="i" :class="['msg', m.role]">
        <div class="bubble">{{ m.content }}</div>
        <div v-if="m.sources?.length" class="sources">
          <span v-for="s in m.sources" :key="s.index" class="src">
            [{{ s.index }}] {{ basename(s.source) }}（精排分 {{ s.score?.toFixed(4) ?? '—' }}）
          </span>
        </div>
      </div>
      <div v-if="streaming" class="msg assistant">
        <div class="bubble typing">▍</div>
      </div>
    </div>

    <div class="input-bar">
      <textarea
        v-model="input"
        placeholder="输入问题，Enter 发送 / Shift+Enter 换行"
        :disabled="streaming"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <button :disabled="streaming || !input.trim()" @click="send">发送</button>
    </div>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: calc(100vh - 100px); }
.head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.new { border: 1px solid #4e6ef2; color: #4e6ef2; background: #fff; border-radius: 6px; padding: 4px 12px; cursor: pointer; }
.messages { flex: 1; overflow-y: auto; background: #fff; border: 1px solid #e5e6eb; border-radius: 10px; padding: 20px; }
.hint { color: #c9cdd4; text-align: center; margin-top: 20vh; line-height: 2; }
.msg { margin-bottom: 16px; display: flex; flex-direction: column; }
.msg.user { align-items: flex-end; }
.bubble {
  max-width: 75%; padding: 10px 14px; border-radius: 10px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word; font-size: 14px;
}
.msg.user .bubble { background: #4e6ef2; color: #fff; border-bottom-right-radius: 3px; }
.msg.assistant .bubble { background: #f6f7fb; border: 1px solid #e5e6eb; border-bottom-left-radius: 3px; }
.sources { margin-top: 6px; display: flex; flex-direction: column; gap: 2px; }
.src { font-size: 12px; color: #86909c; }
.typing { color: #4e6ef2; }
.input-bar { display: flex; gap: 10px; margin-top: 12px; }
textarea {
  flex: 1; resize: none; height: 52px; padding: 12px; border: 1px solid #dcdfe6;
  border-radius: 8px; font-size: 14px; font-family: inherit; outline: none;
}
button {
  width: 84px; border: none; border-radius: 8px; background: #4e6ef2; color: #fff; cursor: pointer;
}
button:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
