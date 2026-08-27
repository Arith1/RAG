<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useFeedback } from '../composables/feedback'

const auth = useAuthStore()
const router = useRouter()
const feedback = useFeedback()

const username = ref('')
const password = ref('')
const confirmPassword = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  const name = username.value.trim()
  if (name.length < 2) {
    error.value = '用户名至少 2 个字符'
    return
  }
  if (password.value.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  if (password.value !== confirmPassword.value) {
    error.value = '两次输入的密码不一致'
    return
  }
  loading.value = true
  try {
    await auth.register(name, password.value)
    feedback.notify('注册成功，欢迎使用共享知识库', 'success')
    router.push('/')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="panel">
      <div class="brand">
        <svg class="mark" viewBox="0 0 64 64" width="40" height="40" aria-hidden="true">
          <rect width="64" height="64" rx="16" fill="var(--accent)" />
          <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="#fff" stroke-width="3.4" stroke-linejoin="round" />
          <path d="M32 15v30" stroke="#fff" stroke-width="3.4" stroke-linecap="round" />
          <path d="M38 7l1.1 3.1L42 11l-2.9.9L38 15l-1.1-3.1L34 11l2.9-.9L38 7Z" fill="#bcd0ff" />
        </svg>
      </div>
      <h1>创建账号</h1>
      <p class="sub">注册后即可上传自己的文档并向智能体提问，也能检索他人共享的材料。</p>

      <form class="form" @submit.prevent="submit" novalidate>
        <label class="field-label" for="username">用户名</label>
        <input
          id="username"
          v-model="username"
          class="field"
          type="text"
          autocomplete="username"
          placeholder="至少 2 个字符"
          maxlength="32"
          @keyup.enter="submit"
        />

        <label class="field-label" for="password">密码</label>
        <input
          id="password"
          v-model="password"
          class="field"
          type="password"
          autocomplete="new-password"
          placeholder="至少 6 位"
          maxlength="64"
          @keyup.enter="submit"
        />

        <label class="field-label" for="confirm">确认密码</label>
        <input
          id="confirm"
          v-model="confirmPassword"
          class="field"
          type="password"
          autocomplete="new-password"
          placeholder="再次输入密码"
          maxlength="64"
          @keyup.enter="submit"
        />

        <p v-if="error" class="error" role="alert">{{ error }}</p>

        <button class="btn btn-primary submit" type="submit" :disabled="loading">
          <span v-if="loading" class="spinner" aria-hidden="true"></span>
          {{ loading ? '注册中…' : '注册' }}
        </button>
      </form>

      <p class="switch-line">
        <span>已有账号？</span>
        <RouterLink to="/login">去登录</RouterLink>
      </p>
      <RouterLink to="/" class="back">← 先逛逛问答界面</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--page-pad);
  background: var(--bg);
}
.panel {
  width: 100%;
  max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  padding: 40px 36px 32px;
}
.brand { display: flex; justify-content: center; margin-bottom: 18px; }
.mark { border-radius: 11px; }
h1 {
  text-align: center;
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.01em;
  margin: 0 0 6px;
}
.sub {
  text-align: center;
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.6;
  margin: 0 0 26px;
}
.form { display: flex; flex-direction: column; }
.field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
  margin: 0 0 6px;
}
.field { margin-bottom: 16px; }
.submit { margin-top: 6px; height: 44px; }
.error {
  color: var(--err);
  font-size: 13px;
  background: var(--err-soft);
  border-radius: var(--radius-sm);
  padding: 9px 12px;
  margin: 0 0 12px;
}
.switch-line {
  text-align: center;
  margin: 20px 0 0;
  font-size: 13px;
  color: var(--text-3);
}
.switch-line a { color: var(--accent); font-weight: 500; }
.switch-line a:hover { text-decoration: underline; }
.back {
  display: block;
  text-align: center;
  margin-top: 12px;
  font-size: 13px;
  color: var(--text-3);
}
.back:hover { color: var(--accent); }

.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.45);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>