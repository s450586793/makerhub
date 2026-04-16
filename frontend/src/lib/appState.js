import { reactive } from "vue";

import { apiRequest } from "./api";
import { avatarText } from "./helpers";
import { applyTheme, getStoredThemePreference, normalizeThemePreference } from "./theme";


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
  appState.githubLatestVersion = String(payload?.github_latest_version || appState.githubLatestVersion || "");
  appState.githubVersionCheckedAt = String(payload?.github_version_checked_at || appState.githubVersionCheckedAt || "");
  appState.githubVersionError = String(payload?.github_version_error || "");
  appState.githubUpdateAvailable = Boolean(payload?.github_update_available);
  if (payload?.theme_preference) {
    const nextPreference = normalizeThemePreference(payload.theme_preference);
    appState.themePreference = nextPreference;
    applyTheme(nextPreference);
  } else {
    applyTheme(appState.themePreference);
  }
  return appState.session;
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


export async function refreshConfig() {
  const payload = await apiRequest("/api/config");
  appState.config = payload;
  appState.appVersion = String(payload?.app_version || appState.appVersion);
  appState.githubLatestVersion = String(payload?.github_latest_version || appState.githubLatestVersion || "");
  appState.githubVersionCheckedAt = String(payload?.github_version_checked_at || appState.githubVersionCheckedAt || "");
  appState.githubVersionError = String(payload?.github_version_error || "");
  appState.githubUpdateAvailable = Boolean(payload?.github_update_available);
  if (appState.session.authenticated) {
    appState.session.username = String(payload?.user?.username || appState.session.username || "");
    appState.session.display_name = String(payload?.user?.display_name || appState.session.display_name || "");
  }
  applyConfigTheme(payload);
  return payload;
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
  const payload = await apiRequest("/api/config/theme", {
    method: "POST",
    body: { theme_preference: normalized },
  });
  appState.config = payload;
  appState.appVersion = String(payload?.app_version || appState.appVersion);
  appState.githubLatestVersion = String(payload?.github_latest_version || appState.githubLatestVersion || "");
  appState.githubVersionCheckedAt = String(payload?.github_version_checked_at || appState.githubVersionCheckedAt || "");
  appState.githubVersionError = String(payload?.github_version_error || "");
  appState.githubUpdateAvailable = Boolean(payload?.github_update_available);
  if (appState.session.authenticated) {
    appState.session.username = String(payload?.user?.username || appState.session.username || "");
    appState.session.display_name = String(payload?.user?.display_name || appState.session.display_name || "");
  }
  appState.themePreference = normalized;
  applyTheme(normalized);
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
