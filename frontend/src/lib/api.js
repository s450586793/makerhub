function buildRedirectTarget() {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

function looksLikeHtmlError(text) {
  const head = String(text || "").trim().slice(0, 1200).toLowerCase();
  if (!head) {
    return false;
  }
  if (head.startsWith("<!doctype html") || head.includes("<html")) {
    return true;
  }
  return /<(html|head|body|script|title|div|meta|style)\b/.test(head);
}

function sanitizeApiError(detail) {
  const text = String(detail || "").trim();
  if (!text) {
    return "请求失败。";
  }
  if (looksLikeHtmlError(text)) {
    const lowered = text.toLowerCase();
    if (["cloudflare", "cf-browser-verification", "cf-chl", "__cf_bm", "cf_clearance"].some((token) => lowered.includes(token))) {
      return "接口返回了风控校验页，通常是 Cookie 失效、代理异常或站点触发了 Cloudflare 校验。";
    }
    return "接口返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。";
  }
  return text.replace(/\s+/g, " ").trim().slice(0, 400);
}


export async function apiRequest(path, options = {}) {
  const {
    method = "GET",
    body = undefined,
    headers = {},
    cache = "no-store",
    redirectOn401 = true,
  } = options;

  const requestHeaders = new Headers(headers);
  let requestBody = body;

  if (body !== undefined && body !== null && !(body instanceof FormData) && typeof body !== "string") {
    requestHeaders.set("Content-Type", "application/json");
    requestBody = JSON.stringify(body);
  }

  if (!requestHeaders.has("Accept")) {
    requestHeaders.set("Accept", "application/json");
  }

  const response = await fetch(path, {
    method,
    headers: requestHeaders,
    body: requestBody,
    credentials: "include",
    cache,
  });

  if (response.status === 401 && redirectOn401) {
    const next = encodeURIComponent(buildRedirectTarget());
    window.location.assign(`/login?next=${next}`);
    throw new Error("未登录。");
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null
      ? payload.detail || payload.message
      : payload;
    throw new Error(sanitizeApiError(detail));
  }

  return payload;
}

export function apiUploadRequest(path, options = {}) {
  const {
    method = "POST",
    body = undefined,
    headers = {},
    redirectOn401 = true,
    onProgress = null,
    onUploadComplete = null,
  } = options;

  return new Promise((resolve, reject) => {
    const requestHeaders = new Headers(headers);
    let requestBody = body;

    if (body !== undefined && body !== null && !(body instanceof FormData) && typeof body !== "string") {
      requestHeaders.set("Content-Type", "application/json");
      requestBody = JSON.stringify(body);
    }

    if (!requestHeaders.has("Accept")) {
      requestHeaders.set("Accept", "application/json");
    }

    const xhr = new XMLHttpRequest();
    xhr.open(method, path, true);
    xhr.withCredentials = true;

    for (const [key, value] of requestHeaders.entries()) {
      xhr.setRequestHeader(key, value);
    }

    xhr.upload.onprogress = (event) => {
      if (typeof onProgress !== "function") {
        return;
      }
      onProgress({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : 0,
        lengthComputable: event.lengthComputable,
        percent: event.lengthComputable && event.total > 0
          ? Math.max(0, Math.min(100, (event.loaded / event.total) * 100))
          : 0,
      });
    };

    xhr.upload.onload = () => {
      if (typeof onUploadComplete === "function") {
        onUploadComplete();
      }
    };

    xhr.onerror = () => reject(new Error("网络请求失败。"));
    xhr.onabort = () => reject(new Error("上传已取消。"));
    xhr.ontimeout = () => reject(new Error("请求超时。"));

    xhr.onload = () => {
      try {
        if (xhr.status === 401 && redirectOn401) {
          const next = encodeURIComponent(buildRedirectTarget());
          window.location.assign(`/login?next=${next}`);
          reject(new Error("未登录。"));
          return;
        }

        if (xhr.status === 204) {
          resolve(null);
          return;
        }

        const contentType = xhr.getResponseHeader("Content-Type") || "";
        const responseText = xhr.responseText || "";
        let payload = responseText;
        if (contentType.includes("application/json")) {
          payload = responseText ? JSON.parse(responseText) : null;
        }

        if (xhr.status < 200 || xhr.status >= 300) {
          const detail = typeof payload === "object" && payload !== null
            ? payload.detail || payload.message
            : payload;
          reject(new Error(sanitizeApiError(detail)));
          return;
        }

        resolve(payload);
      } catch (error) {
        reject(error);
      }
    };

    xhr.send(requestBody);
  });
}
