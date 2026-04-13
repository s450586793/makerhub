const STORAGE_KEY = "makerhub.theme_preference";
const mediaQuery = typeof window !== "undefined" && window.matchMedia
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;


export function normalizeThemePreference(value) {
  return ["light", "dark", "auto"].includes(value) ? value : "auto";
}


export function resolveTheme(preference) {
  const normalized = normalizeThemePreference(preference);
  if (normalized === "light" || normalized === "dark") {
    return normalized;
  }
  return mediaQuery?.matches ? "dark" : "light";
}


export function getStoredThemePreference() {
  try {
    return normalizeThemePreference(window.localStorage.getItem(STORAGE_KEY) || "auto");
  } catch {
    return "auto";
  }
}


export function applyTheme(preference) {
  const normalized = normalizeThemePreference(preference);
  const resolved = resolveTheme(normalized);
  document.documentElement.dataset.themePreference = normalized;
  document.documentElement.dataset.theme = resolved;
  try {
    window.localStorage.setItem(STORAGE_KEY, normalized);
  } catch {
    // ignore localStorage failures
  }
  return { preference: normalized, resolved };
}


export function startThemeObserver() {
  if (!mediaQuery || mediaQuery.__makerhubBound) {
    return;
  }

  const listener = () => {
    if (document.documentElement.dataset.themePreference === "auto") {
      applyTheme("auto");
    }
  };

  mediaQuery.addEventListener("change", listener);
  mediaQuery.__makerhubBound = true;
}
