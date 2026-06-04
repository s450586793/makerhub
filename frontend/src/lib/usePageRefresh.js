import { createPageRefreshScheduler } from "./pageRefresh.js";
import { subscribeStateRefresh } from "./stateEvents.js";

export function createPageRefreshController({
  scopes = [],
  types = [],
  refresh,
  delayMs = 250,
  debounceMs,
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
    isHidden,
  });
  const unsubscribe = typeof subscribe === "function" && scopes.length
    ? subscribe(scopes, (event) => scheduler.schedule(event?.type || "state-event"), {
        types,
        debounceMs: Number.isFinite(Number(debounceMs)) ? Number(debounceMs) : delayMs,
      })
    : null;
  const handleVisibilityChange = () => scheduler.handleVisible();

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
    schedule: (reason) => scheduler.schedule(reason),
  };
}
