<template>
  <div ref="rootRef" class="sidebar-footer">
    <button
      class="user-launcher"
      type="button"
      aria-haspopup="dialog"
      :aria-expanded="String(open)"
      @click.stop="open = !open"
    >
      <span class="user-launcher__avatar">{{ avatar }}</span>
      <span class="user-launcher__meta">
        <strong>{{ displayName }}</strong>
        <small>{{ username }}</small>
      </span>
    </button>

    <div v-if="open" class="user-menu" @click.stop>
      <div class="user-menu__header">
        <span class="user-menu__avatar">{{ avatar }}</span>
        <div>
          <span class="user-menu__role">管理员</span>
          <strong>{{ displayName }}</strong>
          <small>{{ username }}</small>
        </div>
      </div>

      <div class="user-menu__group">
        <RouterLink class="user-menu__link" to="/settings?tab=user" @click="open = false">个人信息</RouterLink>
        <RouterLink class="user-menu__link" to="/settings?tab=system" @click="open = false">系统设置</RouterLink>
      </div>

      <div class="user-menu__theme">
        <div class="user-menu__section-title">主题</div>
        <ThemeSegment :value="themePreference" compact @change="emit('theme-change', $event)" />
      </div>

      <div class="user-menu__group user-menu__group--footer">
        <button class="button button-secondary button-small user-menu__logout" type="button" @click="emit('logout')">
          退出登录
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import ThemeSegment from "./ThemeSegment.vue";


const props = defineProps({
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
});

const emit = defineEmits(["logout", "theme-change"]);

const open = ref(false);
const rootRef = ref(null);

function onDocumentClick(event) {
  if (!rootRef.value?.contains(event.target)) {
    open.value = false;
  }
}

function onDocumentKeydown(event) {
  if (event.key === "Escape") {
    open.value = false;
  }
}

onMounted(() => {
  document.addEventListener("click", onDocumentClick);
  document.addEventListener("keydown", onDocumentKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener("click", onDocumentClick);
  document.removeEventListener("keydown", onDocumentKeydown);
});
</script>
