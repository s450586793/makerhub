import { createRouter, createWebHistory } from "vue-router";

import AppShell from "./layouts/AppShell.vue";
import { appState, bootstrapApp } from "./lib/appState";


const BODY_CLASSES = ["login-page", "detail-page", "detail-page--makerworld"];
const DashboardPage = () => import("./pages/DashboardPage.vue");
const DetailPreviewPage = () => import("./pages/DetailPreviewPage.vue");
const LoginPage = () => import("./pages/LoginPage.vue");
const LogsPage = () => import("./pages/LogsPage.vue");
const ModelDetailPage = () => import("./pages/ModelDetailPage.vue");
const ModelsPage = () => import("./pages/ModelsPage.vue");
const OrganizerPage = () => import("./pages/OrganizerPage.vue");
const RemoteRefreshPage = () => import("./pages/RemoteRefreshPage.vue");
const SettingsPage = () => import("./pages/SettingsPage.vue");
const SubscriptionsPage = () => import("./pages/SubscriptionsPage.vue");
const TasksPage = () => import("./pages/TasksPage.vue");

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
        path: "organizer",
        name: "organizer",
        component: OrganizerPage,
        meta: {
          title: "整理 | makerhub",
        },
      },
      {
        path: "remote-refresh",
        name: "remote-refresh",
        component: RemoteRefreshPage,
        meta: {
          title: "源端刷新 | makerhub",
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
        path: "logs",
        name: "logs",
        component: LogsPage,
        meta: {
          title: "日志 | makerhub",
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
