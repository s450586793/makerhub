<template>
  <div ref="rootRef" class="account-menu" @focusout="onFocusOut">
    <button
      ref="launcherRef"
      class="user-launcher"
      type="button"
      :title="`${displayName} · 账号菜单`"
      :aria-label="`${displayName}，账号菜单${updateAvailable ? '，有新版本可更新' : ''}`"
      aria-haspopup="dialog"
      aria-controls="account-menu-panel"
      :aria-expanded="String(open)"
      @click.stop="toggleMenu"
      @keydown.down.prevent="showMenu"
    >
      <span class="user-launcher__avatar" aria-hidden="true">{{ avatar }}</span>
      <span v-if="updateAvailable" class="account-menu__update-dot" aria-hidden="true" />
    </button>

    <div v-if="open" id="account-menu-panel" class="user-menu" role="dialog" aria-label="账号菜单" @click.stop>
      <div class="user-menu__header">
        <span class="user-menu__avatar" aria-hidden="true">{{ avatar }}</span>
        <div>
          <strong>{{ displayName }}</strong>
          <small>{{ username }}</small>
        </div>
      </div>

      <div class="user-menu__group">
        <RouterLink class="user-menu__link" to="/settings?tab=user" @click="open = false"><UserRound :size="18" aria-hidden="true" />个人信息</RouterLink>
        <RouterLink class="user-menu__link" to="/settings?tab=system" @click="open = false"><Settings :size="18" aria-hidden="true" />系统设置</RouterLink>
      </div>

      <div class="user-menu__theme">
        <div class="user-menu__section-title">主题</div>
        <ThemeSegment :value="themePreference" compact @change="emit('theme-change', $event)" />
      </div>

      <div class="account-menu__versions">
        <div class="account-menu__version">
          <span>当前版本</span>
          <strong>{{ appVersion ? `v${appVersion}` : "读取中" }}</strong>
        </div>
        <RouterLink class="account-menu__version account-menu__version-link" to="/settings?tab=system" title="打开系统更新设置" @click="open = false">
          <span>最新版本</span>
          <strong :class="updateAvailable && 'is-update'">{{ latestVersion }}<ChevronRight :size="14" aria-hidden="true" /></strong>
        </RouterLink>
      </div>

      <div class="user-menu__group user-menu__group--footer">
        <button class="user-menu__link user-menu__logout" type="button" :disabled="logoutPending" @click="emit('logout')">
          <LogOut :size="18" aria-hidden="true" />{{ logoutPending ? "正在退出" : "退出登录" }}
        </button>
        <p v-if="errorMessage" class="account-menu__error" role="alert">{{ errorMessage }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { ChevronRight, LogOut, Settings, UserRound } from "@lucide/vue";

import ThemeSegment from "./ThemeSegment.vue";


defineProps({
  displayName: {
    type: String,
    default: "Admin",
  },
  username: {
    type: String,
    default: "admin",
  },
  avatar: {
    type: String,
    default: "A",
  },
  themePreference: {
    type: String,
    default: "auto",
  },
  appVersion: { type: String, default: "" },
  latestVersion: { type: String, default: "读取中" },
  updateAvailable: { type: Boolean, default: false },
  logoutPending: { type: Boolean, default: false },
  errorMessage: { type: String, default: "" },
});

const emit = defineEmits(["logout", "theme-change"]);

const open = ref(false);
const rootRef = ref(null);
const launcherRef = ref(null);
const route = useRoute();

async function showMenu() {
  open.value = true;
  await nextTick();
  rootRef.value?.querySelector(".user-menu__link")?.focus();
}

function toggleMenu() {
  if (open.value) {
    open.value = false;
  } else {
    void showMenu();
  }
}

function onFocusOut(event) {
  if (event.relatedTarget && !rootRef.value?.contains(event.relatedTarget)) {
    open.value = false;
  }
}

function onDocumentClick(event) {
  if (!rootRef.value?.contains(event.target)) {
    open.value = false;
  }
}

function onDocumentKeydown(event) {
  if (event.key === "Escape" && open.value) {
    open.value = false;
    launcherRef.value?.focus();
  }
}

watch(() => route.fullPath, () => { open.value = false; });

onMounted(() => {
  document.addEventListener("click", onDocumentClick);
  document.addEventListener("keydown", onDocumentKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener("click", onDocumentClick);
  document.removeEventListener("keydown", onDocumentKeydown);
});
</script>
