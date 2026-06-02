export const DEFAULT_STATE_REFRESH_DEBOUNCE_MS = 450;

export function normalizeScopes(scopes) {
  return (Array.isArray(scopes) ? scopes : [scopes])
    .filter((scope) => typeof scope === "string" && scope.length > 0);
}

export function shouldHandleStateEvent(event, scopes = [], types = []) {
  const scopeSet = new Set(normalizeScopes(scopes));
  const typeSet = new Set(normalizeScopes(types));
  const scope = event?.scope || event?.payload?.scope || "";
  const type = event?.type || "";

  if (scopeSet.size && !scopeSet.has(scope) && !scopeSet.has("*")) {
    return false;
  }
  if (typeSet.size && !typeSet.has(type)) {
    return false;
  }
  return true;
}

export function createScopedRefreshScheduler({
  scopes = [],
  types = [],
  callback,
  debounceMs = DEFAULT_STATE_REFRESH_DEBOUNCE_MS,
  isHidden = () => false,
  setTimeoutFn,
  clearTimeoutFn,
} = {}) {
  const delay = Number.isFinite(Number(debounceMs))
    ? Number(debounceMs)
    : DEFAULT_STATE_REFRESH_DEBOUNCE_MS;
  const scheduleTimeout = setTimeoutFn || globalThis.setTimeout;
  const clearScheduledTimeout = clearTimeoutFn || globalThis.clearTimeout;
  let timer = 0;
  let pendingWhenVisible = false;

  const invoke = (event) => {
    if (typeof callback !== "function") {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      return;
    }
    if (timer) {
      clearScheduledTimeout(timer);
    }
    timer = scheduleTimeout(() => {
      timer = 0;
      callback(event);
    }, delay);
  };

  const handleEvent = (event) => {
    if (!shouldHandleStateEvent(event, scopes, types)) {
      return;
    }
    invoke(event);
  };

  const handleVisibilityChange = () => {
    if (isHidden() || !pendingWhenVisible) {
      return;
    }
    pendingWhenVisible = false;
    invoke({ type: "visibility.resumed", scope: "visibility" });
  };

  const dispose = () => {
    pendingWhenVisible = false;
    if (timer) {
      clearScheduledTimeout(timer);
      timer = 0;
    }
  };

  return {
    dispose,
    handleEvent,
    handleVisibilityChange,
  };
}
