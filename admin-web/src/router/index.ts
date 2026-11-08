import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'overview',
    component: () => import('../views/OverviewView.vue'),
    meta: { title: '运行概览' },
  },
  {
    path: '/reports',
    name: 'reports',
    component: () => import('../views/ReportsView.vue'),
    meta: { title: '报告查看' },
  },
  {
    path: '/trigger',
    name: 'trigger',
    component: () => import('../views/TriggerView.vue'),
    meta: { title: '触发报告' },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('../views/JobsView.vue'),
    meta: { title: '任务状态' },
  },
  {
    path: '/traces',
    name: 'traces',
    component: () => import('../views/TracesView.vue'),
    meta: { title: 'AI 调用轨迹' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

// 静态托管无 SPA 回退配置，使用 hash 路由：Caddy try_files 只需提供 /index.html。
export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 复盘了后台` : '复盘了 · 后台管理'
})
