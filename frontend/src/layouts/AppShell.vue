<template>
  <div class="site-shell">
    <aside class="site-sidebar">
      <div class="site-sidebar__inner">
        <RouterLink class="brand-mark" to="/">
          <span class="brand-mark__badge">MH</span>
          <span>
            <strong>makerhub</strong>
            <small>真实归档数据工作台</small>
          </span>
        </RouterLink>

        <nav class="sidebar-nav">
          <RouterLink :class="navClass('/')" to="/">首页</RouterLink>
          <RouterLink :class="navClass('/models')" to="/models">模型库</RouterLink>
          <RouterLink :class="navClass('/settings')" to="/settings">设置</RouterLink>
          <RouterLink :class="navClass('/tasks')" to="/tasks">任务</RouterLink>
        </nav>

        <UserMenu
          :display-name="user.displayName"
          :username="user.username"
          :avatar="user.avatarText"
          :theme-preference="appState.themePreference"
          @logout="handleLogout"
          @theme-change="handleThemeChange"
        />

        <span class="sidebar-version">v{{ appState.appVersion }}</span>
      </div>
    </aside>

    <main class="page-shell">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { computed } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";

import UserMenu from "../components/UserMenu.vue";
import { appState, currentUser, logoutSession, saveThemePreference } from "../lib/appState";


const route = useRoute();

const user = computed(() => currentUser());

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
</script>
