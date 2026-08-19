<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import LoginView from './views/LoginView.vue'

const auth = useAuthStore()
const router = useRouter()

onMounted(async () => {
  // 已有 token 则恢复用户信息
  if (auth.isLoggedIn && !auth.user) {
    try {
      await auth.fetchMe()
    } catch {
      auth.logout()
    }
  }
})

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <div v-if="auth.isLoggedIn" class="layout">
    <header class="topbar">
      <div class="brand">RAG 个人知识库</div>
      <nav>
        <RouterLink to="/" class="link">问答</RouterLink>
        <RouterLink to="/documents" class="link">文档管理</RouterLink>
        <span class="user">{{ auth.user?.username }}（{{ auth.user?.role }}）</span>
        <button class="logout" @click="logout">退出</button>
      </nav>
    </header>
    <main class="content">
      <RouterView />
    </main>
  </div>
  <LoginView v-else />
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  background: #f0f2f5; color: #1f2329;
}
.layout { min-height: 100vh; display: flex; flex-direction: column; }
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 24px; height: 52px; background: #fff; border-bottom: 1px solid #e5e6eb;
}
.brand { font-weight: 700; font-size: 16px; }
nav { display: flex; align-items: center; gap: 16px; }
.link { color: #4e6ef2; text-decoration: none; font-size: 14px; }
.link.router-link-active { font-weight: 600; }
.user { font-size: 13px; color: #86909c; }
.logout {
  border: 1px solid #e5e6eb; background: #fff; color: #4e6ef2;
  border-radius: 6px; padding: 4px 12px; cursor: pointer; font-size: 13px;
}
.content { flex: 1; padding: 24px; max-width: 1100px; width: 100%; margin: 0 auto; }
</style>
