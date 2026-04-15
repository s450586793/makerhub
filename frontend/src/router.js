import { createRouter, createWebHistory } from "vue-router";

import AppShell from "./layouts/AppShell.vue";
import DashboardPage from "./pages/DashboardPage.vue";
import DetailPreviewPage from "./pages/DetailPreviewPage.vue";
import LoginPage from "./pages/LoginPage.vue";
import ModelDetailPage from "./pages/ModelDetailPage.vue";
import ModelsPage from "./pages/ModelsPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import SubscriptionsPage from "./pages/SubscriptionsPage.vue";
import TasksPage from "./pages/TasksPage.vue";
import { appState, bootstrapApp } from "./lib/appState";


const BODY_CLASSES = ["login-page", "detail-page", "detail-page--makerworld"];

const routes = [
  {
    path: "/login",
    name: "login",
    component: LoginPage,
    meta: {
      public: true,
      title: "登录 | makerhub",
      bodyClass: "login-page",
    },
  },
  {
    path: "/",
    component: AppShell,
    children: [
      {
        path: "",
        name: "home",
        component: DashboardPage,
        meta: {
          title: "首页 | makerhub",
        },
      },
      {
        path: "models",
        name: "models",
        component: ModelsPage,
        meta: {
          title: "模型库 | makerhub",
        },
      },
      {
        path: "models/:modelDir(.*)*",
        name: "model-detail",
        component: ModelDetailPage,
        meta: {
          title: "模型详情 | makerhub",
          bodyClass: "detail-page detail-page--makerworld",
        },
      },
      {
        path: "subscriptions",
        name: "subscriptions",
        component: SubscriptionsPage,
        meta: {
          title: "订阅 | makerhub",
        },
      },
      {
        path: "settings",
        name: "settings",
        component: SettingsPage,
        meta: {
          title: "设置 | makerhub",
        },
      },
      {
        path: "tasks",
        name: "tasks",
        component: TasksPage,
        meta: {
          title: "任务 | makerhub",
        },
      },
      {
        path: "detail-preview",
        name: "detail-preview",
        component: DetailPreviewPage,
        meta: {
          title: "详情预览 | makerhub",
        },
      },
    ],
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  await bootstrapApp();

  const isAuthenticated = Boolean(appState.session.authenticated);
  if (to.meta.public) {
    if (isAuthenticated) {
      const next = typeof to.query.next === "string" && to.query.next.startsWith("/")
        ? to.query.next
        : "/";
      return next;
    }
    return true;
  }

  if (!isAuthenticated) {
    const next = encodeURIComponent(to.fullPath || "/");
    return `/login?next=${next}`;
  }

  return true;
});

router.afterEach((to) => {
  document.title = String(to.meta?.title || "makerhub");
  document.body.classList.remove(...BODY_CLASSES);
  String(to.meta?.bodyClass || "")
    .split(/\s+/)
    .filter(Boolean)
    .forEach((className) => document.body.classList.add(className));
});

export default router;
