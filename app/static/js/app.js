async function requestJson(url, options = {}) {
  const method = options.method || "POST";
  const payload = options.payload;
  const response = await fetch(url, {
    method,
    headers: payload !== undefined ? {
      "Content-Type": "application/json",
    } : undefined,
    body: payload !== undefined ? JSON.stringify(payload) : undefined,
  });

  if (!response.ok) {
    let message = "请求失败";
    try {
      const payload = await response.json();
      message = String(payload.detail || payload.message || message);
    } catch (error) {
      const raw = await response.text();
      message = raw || message;
    }
    throw new Error(message);
  }

  return response.json();
}

async function postJson(url, payload) {
  return requestJson(url, { method: "POST", payload });
}

const themeMediaQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function normalizeThemePreference(preference) {
  return ["light", "dark", "auto"].includes(preference) ? preference : "auto";
}

function resolveTheme(preference) {
  const normalized = normalizeThemePreference(preference);
  if (normalized === "light" || normalized === "dark") {
    return normalized;
  }
  return themeMediaQuery?.matches ? "dark" : "light";
}

function applyThemePreference(preference) {
  const normalized = normalizeThemePreference(preference);
  const root = document.documentElement;
  root.dataset.themePreference = normalized;
  root.dataset.theme = resolveTheme(normalized);
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

function buildMaskedValue(rawValue) {
  const clean = String(rawValue || "");
  if (!clean) return "";
  return Array.from(clean, () => "•").join("\u200b");
}

function maskSecretField(textarea) {
  const rawValue = String(textarea.dataset.rawValue || "");
  textarea.dataset.secretMasked = rawValue ? "true" : "false";
  textarea.readOnly = Boolean(rawValue);
  const templateLength = Number.parseInt(textarea.dataset.secretLength || "", 10);
  const maskLength = Number.isFinite(templateLength) && templateLength > 0
    ? templateLength
    : Array.from(rawValue).length;
  textarea.value = rawValue ? Array.from({ length: maskLength }, () => "•").join("\u200b") : "";
}

function unmaskSecretField(textarea) {
  textarea.dataset.secretMasked = "false";
  textarea.readOnly = false;
  textarea.value = String(textarea.dataset.rawValue || "");
}

function readSecretValue(element) {
  if (!element) return "";
  if (element.dataset.secretMasked === "true") {
    return String(element.dataset.rawValue || "");
  }
  const value = String(element.value || "");
  element.dataset.rawValue = value;
  return value;
}

function bindSecretFields() {
  const fields = document.querySelectorAll("[data-secret-input]");
  if (!fields.length) return;

  fields.forEach((textarea) => {
    textarea.dataset.rawValue = String(textarea.defaultValue || textarea.value || "");
    textarea.dataset.secretLength = String(Array.from(textarea.dataset.rawValue).length);

    const wrapper = textarea.closest(".secret-field");
    const toggle = wrapper?.querySelector("[data-secret-toggle]");
    if (!toggle) return;

    if (textarea.dataset.rawValue) {
      maskSecretField(textarea);
      toggle.textContent = "显示";
    } else {
      textarea.dataset.secretMasked = "false";
      textarea.readOnly = false;
      toggle.textContent = "隐藏";
    }

    toggle.addEventListener("click", () => {
      if (textarea.dataset.secretMasked === "true") {
        unmaskSecretField(textarea);
        toggle.textContent = "隐藏";
        textarea.focus();
        return;
      }

      textarea.dataset.rawValue = String(textarea.value || "");
      textarea.dataset.secretLength = String(Array.from(textarea.dataset.rawValue).length);
      maskSecretField(textarea);
      toggle.textContent = "显示";
    });
  });
}

function bindSettingsTabs() {
  const tabs = document.querySelectorAll("[data-settings-tab]");
  const panels = document.querySelectorAll("[data-settings-panel]");
  if (!tabs.length || !panels.length) return;

  const activateTab = (target) => {
    const normalizedTarget = String(target || "").trim();
    const fallback = tabs[0]?.dataset.settingsTab || "";
    const activeTarget = Array.from(tabs).some((tab) => tab.dataset.settingsTab === normalizedTarget)
      ? normalizedTarget
      : fallback;

    tabs.forEach((tab) => tab.classList.toggle("is-active", tab.dataset.settingsTab === activeTarget));
    panels.forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.settingsPanel === activeTarget);
    });
  };

  tabs.forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.settingsTab;
      activateTab(target);
      const url = new URL(window.location.href);
      url.searchParams.set("tab", target);
      window.history.replaceState({}, "", `${url.pathname}?${url.searchParams.toString()}`);
    });
  });

  const initialTarget = new URL(window.location.href).searchParams.get("tab") || window.location.hash.replace(/^#/, "");
  activateTab(initialTarget);
}

function syncThemeToggle(form) {
  const hiddenInput = form?.querySelector('[name="theme_preference"]');
  const buttons = Array.from(form?.querySelectorAll("[data-theme-choice]") || []);
  if (!hiddenInput || !buttons.length) return;

  const value = normalizeThemePreference(String(hiddenInput.value || ""));
  hiddenInput.value = value;
  buttons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.themeChoice === value);
  });
}

function bindThemeControls() {
  const forms = document.querySelectorAll("[data-theme-form]");
  if (!forms.length) return;

  forms.forEach((form) => {
    const hiddenInput = form.querySelector('[name="theme_preference"]');
    const buttons = Array.from(form.querySelectorAll("[data-theme-choice]"));
    if (!hiddenInput || !buttons.length) return;

    syncThemeToggle(form);
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        hiddenInput.value = normalizeThemePreference(String(button.dataset.themeChoice || "auto"));
        syncThemeToggle(form);
        applyThemePreference(hiddenInput.value);
      });
    });
  });

  const handleThemeChange = () => {
    if ((document.documentElement.dataset.themePreference || "auto") === "auto") {
      applyThemePreference("auto");
    }
  };

  if (themeMediaQuery?.addEventListener) {
    themeMediaQuery.addEventListener("change", handleThemeChange);
  } else if (themeMediaQuery?.addListener) {
    themeMediaQuery.addListener(handleThemeChange);
  }
}

function bindUserMenu() {
  const root = document.querySelector("[data-user-menu-root]");
  const toggle = root?.querySelector("[data-user-menu-toggle]");
  const menu = root?.querySelector("[data-user-menu]");
  if (!root || !toggle || !menu) return;

  const closeMenu = () => {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    root.classList.remove("is-open");
  };

  const openMenu = () => {
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    root.classList.add("is-open");
  };

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    if (menu.hidden) {
      openMenu();
      return;
    }
    closeMenu();
  });

  menu.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  document.addEventListener("click", (event) => {
    if (!root.contains(event.target)) {
      closeMenu();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeMenu();
    }
  });
}

async function submitSettingsForm(form) {
  const kind = form.dataset.saveKind;
  const formData = new FormData(form);

  if (kind === "connections") {
    const cookieCn = readSecretValue(form.querySelector('[name="cookie_cn"]'));
    const cookieGlobal = readSecretValue(form.querySelector('[name="cookie_global"]'));
    await postJson("/api/config/cookies", [
      { platform: "cn", cookie: cookieCn },
      { platform: "global", cookie: cookieGlobal },
    ]);
    await postJson("/api/config/proxy", {
      enabled: formData.get("proxy_enabled") === "on",
      http_proxy: String(formData.get("http_proxy") || ""),
      https_proxy: String(formData.get("https_proxy") || ""),
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

  if (kind === "theme") {
    const themePreference = normalizeThemePreference(String(formData.get("theme_preference") || "auto"));
    await postJson("/api/config/theme", {
      theme_preference: themePreference,
    });
    applyThemePreference(themePreference);
    return "主题设置已保存";
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

function bindPasswordForm() {
  const form = document.querySelector("[data-password-form]");
  if (!form) return;

  const status = form.querySelector("[data-password-status]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const currentPassword = String(form.querySelector('[name="current_password"]')?.value || "");
    const newPassword = String(form.querySelector('[name="new_password"]')?.value || "");
    const confirmPassword = String(form.querySelector('[name="confirm_password"]')?.value || "");

    if (!currentPassword || !newPassword) {
      setStatusTarget(status, "请先填写当前密码和新密码。", "is-error");
      return;
    }
    if (newPassword !== confirmPassword) {
      setStatusTarget(status, "两次输入的新密码不一致。", "is-error");
      return;
    }

    setStatusTarget(status, "提交中...", null);
    try {
      const response = await postJson("/api/auth/password", {
        current_password: currentPassword,
        new_password: newPassword,
      });
      setStatusTarget(status, response.message || "密码已更新，请重新登录。", "is-success");
      form.reset();
      window.setTimeout(() => {
        window.location.href = "/login";
      }, 900);
    } catch (error) {
      setStatusTarget(status, error.message || "更新失败", "is-error");
    }
  });
}

function bindTokenManager() {
  const root = document.querySelector("[data-token-manager]");
  if (!root) return;

  const createForm = root.querySelector("[data-token-create-form]");
  const status = root.querySelector("[data-token-status]");
  const output = root.querySelector("[data-token-output]");
  const outputValue = root.querySelector("[data-token-value]");
  const tokenList = root.querySelector("[data-token-list]");

  const renderTokenItem = (item) => {
    if (!tokenList || !item) return;
    const empty = tokenList.querySelector(".empty-copy");
    if (empty) {
      empty.remove();
    }

    const article = document.createElement("article");
    article.className = "token-item";
    article.dataset.tokenId = item.id;
    article.innerHTML = `
      <div>
        <strong>${item.name}</strong>
        <span>${item.token_prefix}...</span>
      </div>
      <div class="token-item__meta">
        <span>创建于 ${item.created_at || "-"}</span>
        <span>最近使用 ${item.last_used_at || "未使用"}</span>
      </div>
      <button class="button button-secondary button-small" type="button" data-token-revoke="${item.id}">撤销</button>
    `;
    tokenList.prepend(article);
    bindRevokeButton(article.querySelector("[data-token-revoke]"));
  };

  const bindRevokeButton = (button) => {
    if (!button) return;
    button.addEventListener("click", async () => {
      const tokenId = String(button.dataset.tokenRevoke || "");
      if (!tokenId) return;
      setStatusTarget(status, "撤销中...", null);
      try {
        await requestJson(`/api/auth/tokens/${tokenId}`, { method: "DELETE" });
        button.closest(".token-item")?.remove();
        if (tokenList && !tokenList.children.length) {
          tokenList.innerHTML = '<p class="empty-copy">当前还没有 API Token。</p>';
        }
        setStatusTarget(status, "Token 已撤销。", "is-success");
      } catch (error) {
        setStatusTarget(status, error.message || "撤销失败", "is-error");
      }
    });
  };

  createForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = String(createForm.querySelector('[name="name"]')?.value || "");
    setStatusTarget(status, "生成中...", null);

    try {
      const response = await postJson("/api/auth/tokens", { name });
      if (output && outputValue) {
        output.hidden = false;
        outputValue.textContent = response.token || "";
      }
      renderTokenItem(response.item);
      setStatusTarget(status, "Token 已生成，请立即保存。", "is-success");
      createForm.reset();
    } catch (error) {
      setStatusTarget(status, error.message || "生成失败", "is-error");
    }
  });

  root.querySelectorAll("[data-token-revoke]").forEach((button) => bindRevokeButton(button));
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

function bindMissing3mfActions() {
  const status = document.querySelector("[data-missing-3mf-status]");
  const retryButtons = Array.from(document.querySelectorAll("[data-missing-retry]"));
  const retryAllButton = document.querySelector("[data-missing-retry-all]");
  if (!status && !retryButtons.length && !retryAllButton) return;

  const withBusyState = async (button, runner) => {
    if (!button) {
      await runner();
      return;
    }
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "处理中...";
    try {
      await runner();
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };

  retryButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      const payload = {
        model_id: String(button.dataset.modelId || ""),
        model_url: String(button.dataset.modelUrl || ""),
        title: String(button.dataset.modelTitle || ""),
      };
      await withBusyState(button, async () => {
        setStatusTarget(status, "正在加入重新下载队列...", null);
        try {
          const response = await postJson("/api/tasks/missing-3mf/retry", payload);
          setStatusTarget(status, response.message || "已加入重新下载队列", "is-success");
          window.setTimeout(() => window.location.reload(), 800);
        } catch (error) {
          setStatusTarget(status, error.message || "重新下载提交失败", "is-error");
        }
      });
    });
  });

  retryAllButton?.addEventListener("click", async () => {
    await withBusyState(retryAllButton, async () => {
      setStatusTarget(status, "正在批量加入重新下载队列...", null);
      try {
        const response = await postJson("/api/tasks/missing-3mf/retry-all", {});
        setStatusTarget(status, response.message || "批量重试已提交", "is-success");
        window.setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        setStatusTarget(status, error.message || "批量重试失败", "is-error");
      }
    });
  });
}

function setImageSource(image, options = {}) {
  if (!image) return;

  const src = String(options.src || options.fallback || "").trim();
  const fallback = String(options.fallback || "").trim();
  const alt = String(options.alt || "").trim();
  if (!src) return;

  if (alt) {
    image.alt = alt;
  }

  if (fallback) {
    image.dataset.fallbackSrc = fallback;
  } else {
    image.removeAttribute("data-fallback-src");
  }

  delete image.dataset.fallbackTried;
  if (image.getAttribute("src") !== src) {
    image.setAttribute("src", src);
  }
}

function bindImageFallbacks(root = document) {
  const images = root.querySelectorAll("img[data-fallback-src]");
  if (!images.length) return;

  images.forEach((image) => {
    if (image.dataset.fallbackBound === "true") return;
    image.dataset.fallbackBound = "true";

    image.addEventListener("error", () => {
      const fallback = String(image.dataset.fallbackSrc || "").trim();
      if (!fallback || image.dataset.fallbackTried === "true") return;
      if (image.currentSrc === fallback || image.src === fallback) return;
      image.dataset.fallbackTried = "true";
      image.src = fallback;
    });
  });
}

function bindDetailGallery() {
  const page = document.querySelector("[data-detail-page]");
  const mainImage = page?.querySelector("[data-detail-main-image]");
  if (!page || !mainImage) return;

  const mediaButtons = Array.from(page.querySelectorAll("[data-detail-media]"));
  const instanceButtons = Array.from(page.querySelectorAll("[data-instance-button]"));
  const instancePanels = Array.from(page.querySelectorAll("[data-instance-panel]"));

  const setActiveInstance = (instanceKey) => {
    instanceButtons.forEach((button) => {
      button.classList.toggle("is-active", button.dataset.instanceButton === instanceKey);
    });

    instancePanels.forEach((panel) => {
      const active = panel.dataset.instancePanel === instanceKey;
      panel.classList.toggle("is-active", active);
      panel.hidden = !active;
    });
  };

  const setActiveMedia = (group, activeButton) => {
    mediaButtons.forEach((button) => {
      if ((button.dataset.mediaGroup || "") !== group) return;
      button.classList.toggle("is-active", button === activeButton);
    });
  };

  const switchMainImage = (button) => {
    const src = String(button.dataset.mediaSrc || "").trim();
    const fallback = String(button.dataset.mediaFallback || "").trim();
    const alt = String(button.dataset.mediaAlt || mainImage.alt || "").trim();
    if (!src && !fallback) return;

    setImageSource(mainImage, { src, fallback, alt });
    bindImageFallbacks(page);
  };

  const activateInstance = (button) => {
    const instanceKey = String(button.dataset.instanceButton || "").trim();
    if (!instanceKey) return;

    setActiveInstance(instanceKey);
    const panel = instancePanels.find((item) => item.dataset.instancePanel === instanceKey);
    const firstMedia = panel?.querySelector("[data-detail-media]");
    if (firstMedia) {
      setActiveMedia(String(firstMedia.dataset.mediaGroup || ""), firstMedia);
      switchMainImage(firstMedia);
      return;
    }

    switchMainImage({
      dataset: {
        mediaSrc: button.dataset.instancePrimarySrc || "",
        mediaFallback: button.dataset.instancePrimaryFallback || "",
        mediaAlt: button.dataset.instancePrimaryAlt || "",
      },
    });
  };

  mediaButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const group = String(button.dataset.mediaGroup || "").trim();
      const instanceKey = String(button.dataset.instanceKey || "").trim();

      if (instanceKey) {
        setActiveInstance(instanceKey);
      }
      if (group) {
        setActiveMedia(group, button);
      }
      switchMainImage(button);
    });
  });

  instanceButtons.forEach((button) => {
    button.addEventListener("click", () => activateInstance(button));
  });
}

function bindLightbox() {
  const lightbox = document.querySelector("[data-lightbox]");
  const lightboxImage = lightbox?.querySelector(".lightbox__image");
  if (!lightbox || !lightboxImage) return;

  const closeLightbox = () => {
    lightbox.hidden = true;
    lightboxImage.removeAttribute("src");
    lightboxImage.removeAttribute("data-fallback-src");
    document.body.classList.remove("is-lightbox-open");
  };

  document.querySelectorAll("[data-lightbox-src]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      const src = String(trigger.dataset.lightboxSrc || "").trim();
      const fallback = String(trigger.dataset.fallbackSrc || "").trim();
      if (!src && !fallback) return;

      lightboxImage.src = src || fallback;
      if (fallback) {
        lightboxImage.dataset.fallbackSrc = fallback;
      } else {
        lightboxImage.removeAttribute("data-fallback-src");
      }
      lightbox.hidden = false;
      document.body.classList.add("is-lightbox-open");
      bindImageFallbacks(lightbox);
    });
  });

  lightbox.querySelectorAll("[data-lightbox-close]").forEach((button) => {
    button.addEventListener("click", closeLightbox);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !lightbox.hidden) {
      closeLightbox();
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  applyThemePreference(document.documentElement.dataset.themePreference || "auto");
  bindSecretFields();
  bindSettingsTabs();
  bindThemeControls();
  bindUserMenu();
  bindSettingsForms();
  bindPasswordForm();
  bindTokenManager();
  bindArchiveForm();
  bindTaskAutoRefresh();
  bindMissing3mfActions();
  bindDetailGallery();
  bindImageFallbacks();
  bindLightbox();
});
