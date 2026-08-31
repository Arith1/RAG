<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import FeedbackLayer from './components/FeedbackLayer.vue'

const auth = useAuthStore()
const router = useRouter()
const restoring = ref(true)

onMounted(async () => {
  // 已有 token 则恢复用户信息
  if (auth.isLoggedIn && !auth.user) {
    try {
      await auth.fetchMe()
    } catch {
      auth.logout()
    }
  }
  restoring.value = false
})

function logout() {
  auth.logout()
  router.push('/')
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="topbar-inner">
        <RouterLink to="/" class="brand" aria-label="共享知识库首页">
          <svg class="mark" viewBox="0 0 64 64" width="26" height="26" aria-hidden="true">
            <rect width="64" height="64" rx="16" fill="var(--accent)" />
            <path d="M32 15c-4-2.6-8.8-4-14-4v30c5.2 0 10 1.4 14 4 4-2.6 8.8-4 14-4V11c-5.2 0-10 1.4-14 4Z" fill="none" stroke="#fff" stroke-width="3.4" stroke-linejoin="round" />
            <path d="M32 15v30" stroke="#fff" stroke-width="3.4" stroke-linecap="round" />
            <path d="M38 7l1.1 3.1L42 11l-2.9.9L38 15l-1.1-3.1L34 11l2.9-.9L38 7Z" fill="#bcd0ff" />
          </svg>
          <span class="brand-name">共享知识库</span>
        </RouterLink>

        <nav class="nav" aria-label="主导航">
          <RouterLink to="/knowledge" class="nav-link" active-class="active">知识库</RouterLink>
          <RouterLink to="/" class="nav-link" active-class="active">问答</RouterLink>
          <RouterLink to="/billing" class="nav-link" active-class="active">用量</RouterLink>
          <RouterLink v-if="auth.isAdmin" to="/obs" class="nav-link" active-class="active">监控</RouterLink>
        </nav>

        <div class="user-area">
          <RouterLink to="/documents" class="nav-link docs-link" active-class="active">文档管理</RouterLink>
          <template v-if="!restoring && auth.isLoggedIn && auth.user">
            <RouterLink
              to="/profile"
              class="user-chip"
              title="查看个人详情"
              :aria-label="`查看 ${auth.user.username} 的个人详情`"
            >
              <span class="avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
              <span class="user-name">{{ auth.user.username }}</span>
              <span class="role" :class="{ admin: auth.isAdmin }">
                {{ auth.isAdmin ? '管理员' : '用户' }}
              </span>
              <svg class="chip-chevron" viewBox="0 0 20 20" width="13" height="13" aria-hidden="true">
                <path d="M7 6l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </RouterLink>
            <button class="btn btn-ghost logout-btn" @click="logout">退出</button>
          </template>
          <template v-else-if="!restoring">
            <RouterLink to="/login" class="btn btn-primary">登录</RouterLink>
          </template>
        </div>
      </div>
    </header>

    <main class="content">
      <RouterView :key="router.currentRoute.value.fullPath" />
    </main>

    <FeedbackLayer />
  </div>
</template>

<style scoped>
.shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
@supports (height: 100dvh) {
  .shell { height: 100dvh; }
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(245, 246, 248, 0.82);
  backdrop-filter: saturate(1.2) blur(10px);
  border-bottom: 1px solid var(--border);
}
.topbar-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 var(--page-pad);
  height: var(--nav-h);
  display: flex;
  align-items: center;
  gap: 28px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: var(--text);
  font-weight: 650;
  font-size: 16px;
  letter-spacing: -0.01em;
}
.mark { flex: none; border-radius: 8px; }
.brand-name { white-space: nowrap; }

.nav {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
}
.nav-link {
  position: relative;
  color: var(--text-2);
  font-size: 14px;
  font-weight: 500;
  padding: 8px 14px;
  border-radius: var(--radius-sm);
  transition: color 0.16s ease, background 0.16s ease;
}
.nav-link:hover { color: var(--text); background: var(--surface-3); }
.nav-link.active { color: var(--accent); background: var(--accent-soft); }

.user-area {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px 5px 5px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--border);
  background: var(--surface);
  font-size: 13px;
  color: var(--text-2);
  transition: border-color 0.16s ease, box-shadow 0.16s ease, color 0.16s ease;
}
.user-chip:hover {
  border-color: var(--border-strong);
  color: var(--text);
  box-shadow: var(--shadow-xs);
}
.chip-chevron { color: var(--text-3); }
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 650;
  font-size: 12px;
}
.user-name { color: var(--text); font-weight: 500; }
.role { font-size: 11px; color: var(--text-3); padding: 2px 7px; border-radius: var(--radius-pill); background: var(--neutral-soft); }
.role.admin { color: var(--accent); background: var(--accent-soft); }

.content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

@media (max-width: 640px) {
  .topbar-inner { padding: 0 14px; gap: 14px; }
  .brand-name { display: none; }
  .user-name { display: none; }
  .logout-btn { padding: 8px 10px; }
}
</style>
