import { createPageRefreshScheduler } from "./pageRefresh.js";
import { subscribeStateRefresh } from "./stateEvents.js";

export function createPageRefreshController({
  scopes = [],
  types = [],
  eventRules = [],
  refresh,
  delayMs = 250,
  debounceMs,
  resetExistingTimer = true,
  refreshOnVisible = false,
  subscribe = subscribeStateRefresh,
  isHidden = () => typeof document !== "undefined" && document.hidden,
  addVisibilityListener = (handler) => {
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handler);
    }
  },
  removeVisibilityListener = (handler) => {
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", handler);
    }
  },
} = {}) {
  const scheduler = createPageRefreshScheduler({
    refresh,
    delayMs,
    resetExistingTimer,
    isHidden,
  });
  const unsubscribe = typeof subscribe === "function" && (scopes.length || eventRules.length)
    ? subscribe(scopes, (event) => scheduler.schedule(event?.type || "state-event"), {
        types,
        eventRules,
        debounceMs: Number.isFinite(Number(debounceMs)) ? Number(debounceMs) : delayMs,
      })
    : null;
  const handleVisibilityChange = () => {
    if (refreshOnVisible && !isHidden()) {
      void scheduler.refreshNow("visibility-resumed");
      return;
    }
    scheduler.handleVisible();
  };

  addVisibilityListener(handleVisibilityChange);

  return {
    clear: () => scheduler.clear(),
    dispose: () => {
      scheduler.dispose();
      if (typeof unsubscribe === "function") {
        unsubscribe();
      }
      removeVisibilityListener(handleVisibilityChange);
    },
    refreshNow: (reason) => scheduler.refreshNow(reason),
    schedule: (reason) => scheduler.schedule(reason),
  };
}
