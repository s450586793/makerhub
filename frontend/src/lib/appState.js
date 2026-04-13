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


export async function refreshSession() {
  const payload = await apiRequest("/api/auth/me", { redirectOn401: false });
  appState.session = {
    authenticated: Boolean(payload?.authenticated),
    kind: String(payload?.kind || ""),
    username: String(payload?.username || ""),
    display_name: String(payload?.display_name || ""),
  };
  return appState.session;
}


export async function refreshConfig() {
  const payload = await apiRequest("/api/config");
  appState.config = payload;
  appState.appVersion = String(payload?.app_version || appState.appVersion);
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
    const session = await refreshSession();
    if (session.authenticated) {
      await refreshConfig();
    } else {
      appState.config = null;
      applyTheme(appState.themePreference);
    }
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
  await apiRequest("/api/config/theme", {
    method: "POST",
    body: { theme_preference: normalized },
  });
  appState.themePreference = normalized;
  applyTheme(normalized);
  await refreshConfig();
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
