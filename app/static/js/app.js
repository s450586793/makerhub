async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || "请求失败");
  }

  return response.json();
}

function setStatusTarget(target, message, type) {
  if (!target) return;
  target.textContent = message;
  target.classList.remove("is-success", "is-error");
  if (type) {
    target.classList.add(type);
  }
}

function setFormStatus(form, message, type) {
  const target = form.querySelector("[data-form-status]");
  setStatusTarget(target, message, type);
}

function bindSettingsTabs() {
  const tabs = document.querySelectorAll("[data-settings-tab]");
  const panels = document.querySelectorAll("[data-settings-panel]");
  if (!tabs.length || !panels.length) return;

  tabs.forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.settingsTab;
      tabs.forEach((tab) => tab.classList.toggle("is-active", tab === button));
      panels.forEach((panel) => {
        panel.classList.toggle("is-active", panel.dataset.settingsPanel === target);
      });
    });
  });
}

async function submitSettingsForm(form) {
  const kind = form.dataset.saveKind;
  const formData = new FormData(form);

  if (kind === "connections") {
    await postJson("/api/config/cookies", [
      { platform: "cn", cookie: String(formData.get("cookie_cn") || "") },
      { platform: "global", cookie: String(formData.get("cookie_global") || "") },
    ]);
    await postJson("/api/config/proxy", {
      enabled: formData.get("proxy_enabled") === "on",
      http_proxy: String(formData.get("http_proxy") || ""),
      https_proxy: String(formData.get("https_proxy") || ""),
      no_proxy: String(formData.get("no_proxy") || ""),
    });
    return "连接设置已保存";
  }

  if (kind === "notifications") {
    await postJson("/api/config/notifications", {
      enabled: formData.get("enabled") === "on",
      telegram_bot_token: String(formData.get("telegram_bot_token") || ""),
      telegram_chat_id: String(formData.get("telegram_chat_id") || ""),
      webhook_url: String(formData.get("webhook_url") || ""),
    });
    return "通知设置已保存";
  }

  if (kind === "user") {
    await postJson("/api/config/user", {
      username: String(formData.get("username") || ""),
      display_name: String(formData.get("display_name") || ""),
      password_hint: String(formData.get("password_hint") || ""),
    });
    return "用户信息已保存";
  }

  if (kind === "organizer") {
    await postJson("/api/config/organizer", {
      source_dir: String(formData.get("source_dir") || ""),
      target_dir: String(formData.get("target_dir") || ""),
      move_files: formData.get("move_files") === "on",
    });
    return "整理配置已保存";
  }

  return "已保存";
}

function bindSettingsForms() {
  const forms = document.querySelectorAll("[data-save-kind]");
  if (!forms.length) return;

  forms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      setFormStatus(form, "保存中...", null);

      try {
        const message = await submitSettingsForm(form);
        setFormStatus(form, message, "is-success");
      } catch (error) {
        setFormStatus(form, error.message || "保存失败", "is-error");
      }
    });
  });
}

function bindArchiveForm() {
  const form = document.querySelector("[data-archive-form]");
  if (!form) return;

  const input = form.querySelector('input[name="url"]');
  const status = document.querySelector("[data-archive-status]");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = String(input?.value || "").trim();

    if (!url) {
      setStatusTarget(status, "请先输入要归档的链接", "is-error");
      return;
    }

    setStatusTarget(status, "提交中...", null);

    try {
      const response = await postJson("/api/archive", { url });
      if (!response.accepted) {
        setStatusTarget(status, response.message || "归档提交失败", "is-error");
        return;
      }

      const message = response.message || "已提交归档任务";
      setStatusTarget(status, message, "is-success");
      form.reset();
      window.setTimeout(() => {
        window.location.reload();
      }, 800);
    } catch (error) {
      setStatusTarget(status, error.message || "归档提交失败", "is-error");
    }
  });
}

function bindTaskAutoRefresh() {
  const page = document.querySelector("[data-tasks-page]");
  if (!page) return;

  const runningCount = Number(page.dataset.runningCount || "0");
  if (!Number.isFinite(runningCount) || runningCount <= 0) return;

  window.setTimeout(() => {
    window.location.reload();
  }, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  bindSettingsTabs();
  bindSettingsForms();
  bindArchiveForm();
  bindTaskAutoRefresh();
});
