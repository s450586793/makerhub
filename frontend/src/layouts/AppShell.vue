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

        <UserMenu
          :display-name="user.displayName"
          :username="user.username"
          :avatar="user.avatarText"
          :theme-preference="appState.themePreference"
          @logout="handleLogout"
          @theme-change="handleThemeChange"
        />

        <div class="sidebar-version">
          <span class="sidebar-version__line">
            <span class="sidebar-version__label">当前</span>
            <span class="sidebar-version__value">{{ appState.appVersion ? `v${appState.appVersion}` : "读取中" }}</span>
          </span>
          <span class="sidebar-version__line">
            <span class="sidebar-version__label">最新版本</span>
            <RouterLink
              class="sidebar-version__link"
              :to="{ path: '/settings', query: { tab: 'system' } }"
              title="打开系统更新设置"
              @click="closeSidebar"
            >
              <span :class="['sidebar-version__value', appState.githubUpdateAvailable && 'is-update']">
                {{ githubVersionText }}
              </span>
            </RouterLink>
          </span>
        </div>
      </div>
    </aside>

    <main class="page-shell">
      <button
        v-if="!sidebarVisible"
        class="shell-visibility-toggle"
        type="button"
        title="显示导航栏"
        aria-label="显示导航栏"
        @click="toggleSidebar"
      >
        显示
      </button>
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";

import UserMenu from "../components/UserMenu.vue";
import { appState, currentUser, logoutSession, refreshVersionStatus, saveThemePreference } from "../lib/appState";


const route = useRoute();
const COMPACT_MEDIA_QUERY = "(max-width: 980px)";
const VERSION_REFRESH_INTERVAL_MS = 60 * 1000;
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
let versionRefreshTimer = 0;
let versionRefreshInFlight = false;
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
  const explicit = normalizeNavContext(route.query.nav_context)
    || normalizeNavContext(route.query.return_context)
    || navContextFromReturnTo(route.query.return_to)
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
  const normalized = String(firstQueryValue(value) || "").trim();
  return Object.prototype.hasOwnProperty.call(NAV_CONTEXT_ROOTS, normalized) ? normalized : "";
}

function navContextFromReturnTo(value) {
  const raw = String(firstQueryValue(value) || "").trim();
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) {
    return "";
  }
  try {
    const url = new URL(raw, "http://makerhub.local");
    const explicit = normalizeNavContext(url.searchParams.get("nav_context"));
    if (explicit) {
      return explicit;
    }
    if (url.pathname.startsWith("/models/state/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/local/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/")) {
      return "subscriptions";
    }
  } catch {
    return "";
  }
  return "";
}

function navContextFromHistoryBack() {
  if (typeof window === "undefined") {
    return "";
  }
  return navContextFromReturnTo(window.history?.state?.back || "");
}

async function handleThemeChange(preference) {
  await saveThemePreference(preference);
}

async function handleLogout() {
  await logoutSession();
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

async function refreshVersionPanel({ force = false } = {}) {
  if (versionRefreshInFlight || !appState.session.authenticated) {
    return;
  }
  versionRefreshInFlight = true;
  try {
    await refreshVersionStatus({ force });
  } catch (error) {
    console.error("版本信息刷新失败", error);
  } finally {
    versionRefreshInFlight = false;
  }
}

function clearVersionRefreshTimer() {
  if (versionRefreshTimer) {
    window.clearInterval(versionRefreshTimer);
    versionRefreshTimer = 0;
  }
}

function startVersionRefreshTimer() {
  clearVersionRefreshTimer();
  if (typeof window === "undefined") {
    return;
  }
  versionRefreshTimer = window.setInterval(() => {
    if (!document.hidden) {
      void refreshVersionPanel({ force: true });
    }
  }, VERSION_REFRESH_INTERVAL_MS);
}

function onDocumentVisibilityChange() {
  if (!document.hidden) {
    void refreshVersionPanel({ force: false });
  }
}

function onWindowFocus() {
  void refreshVersionPanel({ force: false });
}

watch(() => route.fullPath, () => {
  closeSidebar();
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
  window.addEventListener("focus", onWindowFocus);
  document.addEventListener("visibilitychange", onDocumentVisibilityChange);
  startVersionRefreshTimer();
  void refreshVersionPanel({ force: false });
});

onBeforeUnmount(() => {
  clearVersionRefreshTimer();
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
  window.removeEventListener("focus", onWindowFocus);
  document.removeEventListener("visibilitychange", onDocumentVisibilityChange);
});
</script>
