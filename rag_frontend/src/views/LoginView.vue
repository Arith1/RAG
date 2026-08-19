<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  error.value = ''
  if (!username.value || !password.value) return
  loading.value = true
  try {
    await auth.login(username.value, password.value)
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
    <div class="card">
      <h1>RAG 个人知识库</h1>
      <p class="sub">登录后开始知识库问答</p>
      <input v-model="username" placeholder="用户名" @keyup.enter="submit" />
      <input v-model="password" type="password" placeholder="密码" @keyup.enter="submit" />
      <button :disabled="loading" @click="submit">{{ loading ? '登录中…' : '登 录' }}</button>
      <p v-if="error" class="err">{{ error }}</p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #4e6ef2 0%, #7b8cff 100%);
}
.card {
  background: #fff; border-radius: 12px; padding: 36px; width: 340px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2);
}
h1 { font-size: 20px; margin-bottom: 4px; }
.sub { color: #86909c; font-size: 13px; margin-bottom: 20px; }
input {
  width: 100%; padding: 10px 12px; margin-bottom: 12px; border: 1px solid #dcdfe6;
  border-radius: 8px; font-size: 14px; outline: none;
}
button {
  width: 100%; padding: 10px; background: #4e6ef2; color: #fff; border: none;
  border-radius: 8px; font-size: 15px; cursor: pointer;
}
button:disabled { opacity: 0.6; cursor: not-allowed; }
.err { color: #f53f3f; font-size: 13px; margin-top: 10px; }
</style>
