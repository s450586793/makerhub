import { reactive } from "vue";

import { apiRequest } from "./api.js";
import { avatarText } from "./helpers.js";
import { applyTheme, getStoredThemePreference, normalizeThemePreference } from "./theme.js";


export const appState = reactive({
  ready: false,
  bootstrapping: false,
  session: {
    authenticated: false,
    kind: "",
    username: "",
    display_name: "",
  },
  config: null,
  appVersion: "",
  githubLatestVersion: "",
  githubVersionCheckedAt: "",
  githubVersionError: "",
  githubUpdateAvailable: false,
  themePreference: getStoredThemePreference(),
});

let bootstrapPromise = null;
let versionStatusPromise = null;
const APP_VERSION_RELOAD_KEY = "makerhub:last-reloaded-version";


function maybeReloadForVersion(nextVersion) {
  if (typeof window === "undefined") {
    return;
  }
  const normalizedNext = String(nextVersion || "").trim();
  const currentVersion = String(appState.appVersion || "").trim();
  if (!appState.ready || !normalizedNext || !currentVersion || normalizedNext === currentVersion) {
    return;
  }
  try {
    const lastReloadedVersion = window.sessionStorage.getItem(APP_VERSION_RELOAD_KEY) || "";
    if (lastReloadedVersion === normalizedNext) {
      return;
    }
    window.sessionStorage.setItem(APP_VERSION_RELOAD_KEY, normalizedNext);
  } catch (error) {
    console.warn("记录版本刷新状态失败", error);
  }
  window.location.reload();
}


function applyConfigTheme(config) {
  const nextPreference = normalizeThemePreference(
    config?.user?.theme_preference || appState.themePreference || "auto",
  );
  appState.themePreference = nextPreference;
  applyTheme(nextPreference);
}


function applyBootstrap(payload) {
  const session = payload?.session || {};
  appState.session = {
    authenticated: Boolean(session?.authenticated),
    kind: String(session?.kind || ""),
    username: String(session?.username || ""),
    display_name: String(session?.display_name || ""),
  };
  appState.appVersion = String(payload?.app_version || appState.appVersion);
  if (payload?.theme_preference) {
    const nextPreference = normalizeThemePreference(payload.theme_preference);
    appState.themePreference = nextPreference;
    applyTheme(nextPreference);
  } else {
    applyTheme(appState.themePreference);
  }
  return appState.session;
}


export function applyVersionPayload(payload) {
  const nextVersion = String(payload?.app_version || appState.appVersion);
  maybeReloadForVersion(nextVersion);
  appState.appVersion = nextVersion;
  appState.githubLatestVersion = String(payload?.github_latest_version ?? "");
  appState.githubVersionCheckedAt = String(payload?.github_version_checked_at ?? "");
  appState.githubVersionError = String(payload?.github_version_error || "");
  appState.githubUpdateAvailable = Boolean(payload?.github_update_available);
  return payload;
}


export async function refreshBootstrap() {
  const payload = await apiRequest("/api/bootstrap", { redirectOn401: false });
  const session = applyBootstrap(payload);
  if (!session.authenticated) {
    appState.config = null;
  }
  return payload;
}


export async function refreshSession() {
  const payload = await refreshBootstrap();
  return payload.session;
}

export function applyLoginSession(payload = {}) {
  appState.session = {
    authenticated: true,
    kind: "session",
    username: String(payload?.username || appState.session.username || "admin"),
    display_name: String(payload?.display_name || appState.session.display_name || ""),
  };
  appState.ready = true;
  appState.bootstrapping = false;
  return appState.session;
}

export function applyConfigPayload(payload) {
  appState.config = payload;
  applyVersionPayload(payload);
  if (appState.session.authenticated) {
    appState.session.username = String(payload?.user?.username || appState.session.username || "");
    appState.session.display_name = String(payload?.user?.display_name || appState.session.display_name || "");
  }
  applyConfigTheme(payload);
  return payload;
}


export async function refreshConfig() {
  return applyConfigPayload(await apiRequest("/api/config"));
}


export async function refreshLightConfig() {
  return applyConfigPayload(await apiRequest("/api/config/light"));
}


export async function refreshVersionStatus(options = {}) {
  const { force = false } = options;
  const query = force ? "?force=true" : "";
  return applyVersionPayload(await apiRequest(`/api/system/version${query}`));
}


export function refreshVersionStatusInBackground(options = {}) {
  if (versionStatusPromise) {
    return versionStatusPromise;
  }
  versionStatusPromise = refreshVersionStatus(options).catch((error) => {
    appState.githubVersionError = String(error?.message || "版本状态读取失败");
    return null;
  }).finally(() => {
    versionStatusPromise = null;
  });
  return versionStatusPromise;
}


export async function bootstrapApp(options = {}) {
  const { force = false } = options;
  if (appState.ready && !force) {
    return appState;
  }
  if (!force && bootstrapPromise) {
    return bootstrapPromise;
  }

  bootstrapPromise = (async () => {
    appState.bootstrapping = true;
    await refreshBootstrap();
    appState.ready = true;
    appState.bootstrapping = false;
    return appState;
  })();

  try {
    return await bootstrapPromise;
  } finally {
    bootstrapPromise = null;
  }
}


export function currentUser() {
  return {
    displayName: appState.session.display_name || appState.session.username || "Admin",
    username: appState.session.username || "admin",
    avatarText: avatarText(appState.session.display_name, appState.session.username),
  };
}


export async function saveThemePreference(preference) {
  const normalized = normalizeThemePreference(preference);
  const payload = applyConfigPayload(await apiRequest("/api/config/theme", {
    method: "POST",
    body: { theme_preference: normalized },
  }));
  appState.themePreference = normalized;
  applyTheme(normalized);
  return payload;
}


export async function logoutSession() {
  await apiRequest("/api/auth/logout", {
    method: "POST",
    redirectOn401: false,
  });
  appState.ready = false;
  appState.config = null;
  appState.session = {
    authenticated: false,
    kind: "",
    username: "",
    display_name: "",
  };
  window.location.assign("/login");
}
