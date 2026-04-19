<template>
  <div :class="['site-shell', !sidebarVisible && 'site-shell--sidebar-hidden', isCompact && 'site-shell--compact']">
    <div v-if="isCompact && sidebarVisible" class="site-sidebar-backdrop" @click="closeSidebar" />

    <aside :class="['site-sidebar', sidebarVisible && 'is-open', isCompact && 'is-compact']">
      <div class="site-sidebar__inner">
        <div class="site-sidebar__head">
          <RouterLink class="brand-mark" to="/" @click="closeSidebar">
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
          <RouterLink :class="navClass('/subscriptions')" to="/subscriptions" @click="closeSidebar">订阅</RouterLink>
          <RouterLink :class="navClass('/settings')" to="/settings" @click="closeSidebar">设置</RouterLink>
          <RouterLink :class="navClass('/organizer')" to="/organizer" @click="closeSidebar">本地整理</RouterLink>
          <RouterLink :class="navClass('/remote-refresh')" to="/remote-refresh" @click="closeSidebar">源端刷新</RouterLink>
          <RouterLink :class="navClass('/tasks')" to="/tasks" @click="closeSidebar">归档任务</RouterLink>
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
            <a
              class="sidebar-version__link"
              :href="githubProjectUrl"
              target="_blank"
              rel="noreferrer noopener"
              title="打开 makerhub GitHub 仓库"
            >
              <span :class="['sidebar-version__value', appState.githubUpdateAvailable && 'is-update']">
                {{ githubVersionText }}
              </span>
            </a>
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
import { appState, currentUser, logoutSession, saveThemePreference } from "../lib/appState";


const route = useRoute();
const COMPACT_MEDIA_QUERY = "(max-width: 980px)";
const githubProjectUrl = "https://github.com/s450586793/makerhub";

const user = computed(() => currentUser());
const isCompact = ref(false);
const desktopSidebarHidden = ref(false);
const mobileSidebarOpen = ref(false);

let compactMediaQuery = null;
let mediaListener = null;

const sidebarVisible = computed(() => (
  isCompact.value ? mobileSidebarOpen.value : !desktopSidebarHidden.value
));
const githubVersionText = computed(() => {
  if (appState.githubLatestVersion) {
    return `v${appState.githubLatestVersion}`;
  }
  if (appState.githubVersionError) {
    return "读取失败";
  }
  return "读取中";
});

function navClass(prefix) {
  const active = prefix === "/"
    ? route.path === "/"
    : route.path === prefix || route.path.startsWith(`${prefix}/`);
  return ["sidebar-nav__link", active && "is-active"];
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
  }
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
  }
}

function onWindowKeydown(event) {
  if (event.key === "Escape") {
    closeSidebar();
  }
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
});

onBeforeUnmount(() => {
  document.body.classList.remove("sidebar-overlay-open");
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
