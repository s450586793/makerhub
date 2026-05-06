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
    return normalizeThemePreference(window.localStorage.getItem(STORAGE_KEY));
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
  const handleThemeChange = () => {
    if (document.documentElement.dataset.themePreference === "auto") {
      applyTheme("auto");
    }
  };

  if (mediaQuery?.addEventListener) {
    mediaQuery.addEventListener("change", handleThemeChange);
  } else if (mediaQuery?.addListener) {
    mediaQuery.addListener(handleThemeChange);
  }
}
