const STORAGE_KEY = "makerhub.theme_preference";


export function normalizeThemePreference(value) {
  return "light";
}


export function resolveTheme(preference) {
  return "light";
}


export function getStoredThemePreference() {
  return "light";
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
  applyTheme("light");
}
