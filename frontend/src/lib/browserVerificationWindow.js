const WINDOW_NAME = "makerhub-3mf-verification";
const WINDOW_FEATURES = "popup=yes,width=560,height=620,left=120,top=60";

export function browserVerificationPath(sessionId) {
  return `/browser-verification/${encodeURIComponent(String(sessionId || ""))}`;
}

export function reserveBrowserVerificationWindow(browserWindow = globalThis.window) {
  const popup = browserWindow?.open?.("about:blank", WINDOW_NAME, WINDOW_FEATURES);
  if (popup?.document) {
    popup.document.title = "3MF 验证 | makerhub";
    if (popup.document.body) {
      popup.document.body.innerHTML = "正在创建 3MF 验证会话...";
    }
  }
  return popup || null;
}

export function navigateBrowserVerificationWindow(popup, sessionId) {
  if (!popup || popup.closed) {
    return false;
  }
  popup.location.href = browserVerificationPath(sessionId);
  if (typeof popup.focus === "function") {
    popup.focus();
  }
  return true;
}

export function closeBrowserVerificationWindow(popup) {
  if (popup && !popup.closed && typeof popup.close === "function") {
    popup.close();
  }
}

export function openBrowserVerificationWindow(sessionId, browserWindow = globalThis.window) {
  const popup = reserveBrowserVerificationWindow(browserWindow);
  return navigateBrowserVerificationWindow(popup, sessionId);
}
