<template>
  <div :class="shellClass">
    <div v-if="isCompact && sidebarVisible" class="site-sidebar-backdrop" @click="closeSidebar" />

    <aside :class="['site-sidebar', sidebarVisible && 'is-open', isCompact && 'is-compact']">
      <div ref="sidebarInnerRef" class="site-sidebar__inner">
        <div class="site-sidebar__head">
          <RouterLink class="brand-mark" to="/" @click="closeSidebar">
            <img class="brand-mark__logo" :src="logoUrl" alt="" aria-hidden="true">
            <span>
              <strong>makerhub</strong>
              <small>真实归档数据工作台</small>
            </span>
          </RouterLink>
          <button
            class="sidebar-visibility-toggle"
            type="button"
            title="隐藏导航栏"
            aria-label="隐藏导航栏"
            @click="toggleSidebar"
          >
            隐藏
          </button>
        </div>

        <nav class="sidebar-nav">
          <RouterLink :class="navClass('/')" to="/" @click="closeSidebar">首页</RouterLink>
          <RouterLink :class="navClass('/models')" to="/models" @click="closeSidebar">模型库</RouterLink>
          <RouterLink :class="navClass('/subscriptions')" to="/subscriptions" @click="closeSidebar">订阅库</RouterLink>
          <RouterLink :class="navClass('/organizer')" to="/organizer" @click="closeSidebar">本地库</RouterLink>
          <RouterLink :class="navClass('/remote-refresh')" to="/remote-refresh" @click="closeSidebar">源端刷新</RouterLink>
          <RouterLink :class="navClass('/tasks')" to="/tasks" @click="closeSidebar">归档任务</RouterLink>
          <RouterLink :class="navClass('/settings')" to="/settings" @click="closeSidebar">设置</RouterLink>
          <RouterLink :class="navClass('/logs')" to="/logs" @click="closeSidebar">日志</RouterLink>
        </nav>
      </div>
    </aside>

    <main class="page-shell page-shell--account-header">
      <header class="shell-topbar">
        <button
          v-if="!sidebarVisible"
          class="shell-topbar__nav"
          type="button"
          title="显示导航栏"
          aria-label="显示导航栏"
          @click="toggleSidebar"
        >
          <PanelLeft :size="20" aria-hidden="true" />
        </button>
        <form v-if="route.name === 'model-detail'" class="shell-search" role="search" @submit.prevent="searchLibrary">
          <Search :size="18" aria-hidden="true" />
          <input v-model="searchQuery" type="search" aria-label="搜索模型库" placeholder="搜索模型、作者和标签">
          <button type="submit" aria-label="搜索" title="搜索"><ArrowRight :size="18" aria-hidden="true" /></button>
        </form>
        <UserMenu
          :display-name="user.displayName"
          :username="user.username"
          :avatar="user.avatarText"
          :theme-preference="appState.themePreference"
          :app-version="appState.appVersion"
          :latest-version="githubVersionText"
          :update-available="appState.githubUpdateAvailable"
          :logout-pending="logoutPending"
          :error-message="accountError"
          @logout="handleLogout"
          @theme-change="handleThemeChange"
        />
      </header>
      <RouterView v-slot="{ Component, route: currentRoute }">
        <KeepAlive :max="5">
          <component
            :is="Component"
            v-if="currentRoute.meta.keepAlive"
            :key="String(currentRoute.name || currentRoute.path)"
          />
        </KeepAlive>
        <component
          :is="Component"
          v-if="!currentRoute.meta.keepAlive"
        />
      </RouterView>
    </main>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";
import { ArrowRight, PanelLeft, Search } from "@lucide/vue";

import UserMenu from "../components/UserMenu.vue";
import "../styles/account-menu.css";
import { appState, currentUser, logoutSession, refreshVersionStatusInBackground, saveThemePreference } from "../lib/appState";
import { getStoredModelReturnState, inferModelReturnContext, normalizeModelReturnContext } from "../lib/modelNavigation";


const route = useRoute();
const router = useRouter();
const searchQuery = ref("");
const logoutPending = ref(false);
const accountError = ref("");
const COMPACT_MEDIA_QUERY = "(max-width: 980px)";
const logoUrl = "/static/img/makerhub-logo.png";
const NAV_CONTEXT_ROOTS = {
  subscriptions: "/subscriptions",
  organizer: "/organizer",
};

const user = computed(() => currentUser());
const isCompact = ref(false);
const desktopSidebarHidden = ref(false);
const mobileSidebarOpen = ref(false);
const sidebarInnerRef = ref(null);

let compactMediaQuery = null;
let mediaListener = null;
let sidebarResetFrame = 0;

const sidebarVisible = computed(() => (
  isCompact.value ? mobileSidebarOpen.value : !desktopSidebarHidden.value
));
const desktopSidebarCollapsed = computed(() => !isCompact.value && desktopSidebarHidden.value);
const mobileSidebarClosed = computed(() => isCompact.value && !mobileSidebarOpen.value);
const shellClass = computed(() => [
  "site-shell",
  desktopSidebarCollapsed.value && "site-shell--sidebar-hidden",
  isCompact.value && "site-shell--compact",
  mobileSidebarClosed.value && "site-shell--mobile-sidebar-closed",
]);
const githubVersionText = computed(() => {
  if (appState.githubLatestVersion) {
    return `v${appState.githubLatestVersion}`;
  }
  if (appState.githubVersionError) {
    return "读取失败";
  }
  return "读取中";
});
const activeNavRoot = computed(() => {
  const storedReturnState = route.path.startsWith("/models/")
    ? getStoredModelReturnState(browserSessionStorage(), route.path)
    : {};
  const explicit = normalizeNavContext(route.query.nav_context)
    || normalizeNavContext(route.query.return_context)
    || navContextFromReturnTo(route.query.return_to)
    || normalizeNavContext(storedReturnState.returnContext)
    || navContextFromReturnTo(storedReturnState.returnTo)
    || navContextFromHistoryBack();
  if (explicit) {
    return NAV_CONTEXT_ROOTS[explicit] || "";
  }
  if (route.name === "model-library-state") {
    return NAV_CONTEXT_ROOTS.organizer;
  }
  if (route.name === "model-library-source") {
    return String(route.params.sourceType || "") === "local"
      ? NAV_CONTEXT_ROOTS.organizer
      : NAV_CONTEXT_ROOTS.subscriptions;
  }
  return "";
});

function navClass(prefix) {
  const activeRoot = activeNavRoot.value;
  const active = activeRoot
    ? prefix === activeRoot
    : prefix === "/"
    ? route.path === "/"
    : route.path === prefix || route.path.startsWith(`${prefix}/`);
  return ["sidebar-nav__link", active && "is-active"];
}

function firstQueryValue(value) {
  return Array.isArray(value) ? value[0] : value;
}

function normalizeNavContext(value) {
  return normalizeModelReturnContext(firstQueryValue(value));
}

function navContextFromReturnTo(value) {
  return inferModelReturnContext(firstQueryValue(value));
}

function navContextFromHistoryBack() {
  if (typeof window === "undefined") {
    return "";
  }
  return navContextFromReturnTo(window.history?.state?.back || "");
}

function browserSessionStorage() {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

async function handleThemeChange(preference) {
  accountError.value = "";
  try {
    await saveThemePreference(preference);
  } catch {
    accountError.value = "主题保存失败，请重试。";
  }
}

async function handleLogout() {
  if (logoutPending.value) {
    return;
  }
  accountError.value = "";
  logoutPending.value = true;
  try {
    await logoutSession();
  } catch {
    accountError.value = "退出失败，请重试。";
  } finally {
    logoutPending.value = false;
  }
}

function searchLibrary() {
  const query = searchQuery.value.trim();
  router.push({ path: "/models", query: query ? { q: query } : {} });
}

function applyCompact(matches) {
  isCompact.value = matches;
  if (matches) {
    mobileSidebarOpen.value = false;
    resetSidebarScroll();
    return;
  }
  mobileSidebarOpen.value = false;
  resetSidebarScroll();
}

function toggleSidebar() {
  if (isCompact.value) {
    mobileSidebarOpen.value = !mobileSidebarOpen.value;
    return;
  }
  desktopSidebarHidden.value = !desktopSidebarHidden.value;
}

function closeSidebar() {
  if (isCompact.value) {
    mobileSidebarOpen.value = false;
    resetSidebarScroll();
  }
}

function resetSidebarScroll() {
  if (typeof window === "undefined") {
    return;
  }
  if (sidebarResetFrame) {
    window.cancelAnimationFrame(sidebarResetFrame);
  }
  sidebarResetFrame = window.requestAnimationFrame(() => {
    sidebarResetFrame = 0;
    if (!sidebarInnerRef.value) {
      return;
    }
    sidebarInnerRef.value.scrollTop = 0;
    sidebarInnerRef.value.scrollLeft = 0;
  });
}

function onWindowKeydown(event) {
  if (event.key === "Escape") {
    closeSidebar();
  }
}

watch(() => route.fullPath, () => {
  closeSidebar();
  searchQuery.value = "";
});

watch(sidebarVisible, (visible) => {
  document.body.classList.toggle("sidebar-overlay-open", Boolean(isCompact.value && visible));
});

watch(isCompact, (compact) => {
  if (!compact) {
    document.body.classList.remove("sidebar-overlay-open");
  }
});

onMounted(() => {
  void refreshVersionStatusInBackground();

  if (typeof window === "undefined") {
    return;
  }
  compactMediaQuery = window.matchMedia(COMPACT_MEDIA_QUERY);
  applyCompact(compactMediaQuery.matches);

  mediaListener = (event) => applyCompact(event.matches);
  if (typeof compactMediaQuery.addEventListener === "function") {
    compactMediaQuery.addEventListener("change", mediaListener);
  } else if (typeof compactMediaQuery.addListener === "function") {
    compactMediaQuery.addListener(mediaListener);
  }

  window.addEventListener("keydown", onWindowKeydown);
});

onBeforeUnmount(() => {
  document.body.classList.remove("sidebar-overlay-open");
  if (sidebarResetFrame) {
    window.cancelAnimationFrame(sidebarResetFrame);
    sidebarResetFrame = 0;
  }
  if (compactMediaQuery && mediaListener) {
    if (typeof compactMediaQuery.removeEventListener === "function") {
      compactMediaQuery.removeEventListener("change", mediaListener);
    } else if (typeof compactMediaQuery.removeListener === "function") {
      compactMediaQuery.removeListener(mediaListener);
    }
  }
  window.removeEventListener("keydown", onWindowKeydown);
});
</script>
