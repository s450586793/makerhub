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
  bindSecretFields();
  bindSettingsTabs();
  bindSettingsForms();
  bindArchiveForm();
  bindTaskAutoRefresh();
  bindDetailGallery();
  bindImageFallbacks();
  bindLightbox();
});
