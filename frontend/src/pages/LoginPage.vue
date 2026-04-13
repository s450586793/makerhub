<template>
  <div class="site-shell site-shell--no-sidebar">
    <main class="page-shell page-shell--full">
      <section class="login-shell">
        <article class="login-card">
          <div class="login-card__header">
            <div class="brand-mark">
              <span class="brand-mark__badge">MH</span>
              <span>
                <strong>makerhub</strong>
                <small>真实归档数据工作台</small>
              </span>
            </div>
            <div>
              <h1>登录</h1>
              <p>公网访问默认需要先登录，再进入归档、设置与本地模型页面。</p>
            </div>
          </div>

          <form class="login-form" @submit.prevent="submit">
            <label class="field-card">
              <span>用户名</span>
              <input v-model.trim="username" type="text" autocomplete="username" placeholder="admin">
            </label>
            <label class="field-card">
              <span>密码</span>
              <input v-model="password" type="password" autocomplete="current-password" placeholder="请输入密码">
            </label>
            <div class="form-footer">
              <button class="button button-primary" type="submit" :disabled="submitting">
                {{ submitting ? "登录中..." : "登录" }}
              </button>
              <span class="form-status">{{ status }}</span>
            </div>
          </form>
        </article>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { useRoute } from "vue-router";

import { bootstrapApp } from "../lib/appState";
import { apiRequest } from "../lib/api";
import { safeNextPath } from "../lib/helpers";


const route = useRoute();

const username = ref("admin");
const password = ref("");
const status = ref("");
const submitting = ref(false);

async function submit() {
  submitting.value = true;
  status.value = "";

  try {
    await apiRequest("/api/auth/login", {
      method: "POST",
      body: {
        username: username.value,
        password: password.value,
      },
      redirectOn401: false,
    });
    await bootstrapApp({ force: true });
    window.location.assign(safeNextPath(route.query.next || "/"));
  } catch (error) {
    status.value = error instanceof Error ? error.message : "登录失败。";
  } finally {
    submitting.value = false;
  }
}
</script>
