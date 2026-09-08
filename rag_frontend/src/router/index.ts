import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/register', name: 'register', component: () => import('../views/RegisterView.vue') },
    { path: '/', name: 'chat', component: () => import('../views/ChatView.vue') },
    { path: '/documents', name: 'documents', component: () => import('../views/DocumentsView.vue') },
    { path: '/knowledge', name: 'knowledge', component: () => import('../views/KnowledgeBaseView.vue') },
    { path: '/billing', name: 'billing', component: () => import('../views/BillingView.vue') },
    { path: '/obs', name: 'obs', component: () => import('../views/ObsView.vue') },
    { path: '/profile', name: 'profile', component: () => import('../views/ProfileView.vue') },
    { path: '/profile/:userId', name: 'profile-user', component: () => import('../views/ProfileView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

// 已登录用户访问登录页时直接回到问答首页；个人详情/用量/监控需登录，监控仅管理员
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if ((to.name === 'login' || to.name === 'register') && auth.isLoggedIn) {
    return { name: 'chat' }
  }
  if (to.name === 'billing' || to.name === 'obs') {
    if (!auth.isLoggedIn) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
    // M27：监控页仅管理员；role 未知时先回源用户信息再判断
    if (to.name === 'obs' && !auth.user) {
      try {
        await auth.fetchMe()
      } catch {
        /* api() 已处理 401；其余错误保留登录态 */
      }
    }
    if (to.name === 'obs' && !auth.isAdmin) {
      return { name: 'chat' }
    }
  }
  if (to.name === 'profile' || to.name === 'profile-user') {
    if (!auth.isLoggedIn) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
    // 首次进入个人详情时恢复用户信息，确保 /profile 能归一化为 /profile/:id，
    // 避免 targetId 落到 0 导致 404
    if (!auth.user) {
      try {
        await auth.fetchMe()
      } catch (e) {
        // M24：仅 401 视为登录失效；网络/5xx 瞬时错误保留 token，不误登出
        if (e instanceof ApiError && e.status === 401) {
          return { name: 'login', query: { redirect: to.fullPath } }
        }
        return true
      }
    }
    if (to.name === 'profile' && auth.user?.id) {
      // /profile 归一化为 /profile/:id，保证深链与他人视图逻辑一致
      return { name: 'profile-user', params: { userId: String(auth.user.id) }, replace: true }
    }
  }
  return true
})

export default router
