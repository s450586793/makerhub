import {
  createScopedRefreshScheduler,
  DEFAULT_STATE_REFRESH_DEBOUNCE_MS,
} from "./stateRefresh.js";

const EVENT_SOURCE_URL = "/api/events/state";
const RECONNECT_DELAY_MS = 5000;

let eventSource = null;
let reconnectTimer = 0;
let subscriberId = 0;
let lastEventId = 0;
const subscribers = new Map();

function ensureStateEventSource() {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    return;
  }
  if (eventSource || !subscribers.size) {
    return;
  }

  const eventUrl = lastEventId > 0
    ? `${EVENT_SOURCE_URL}?last_event_id=${encodeURIComponent(String(lastEventId))}`
    : EVENT_SOURCE_URL;
  eventSource = new EventSource(eventUrl);
  const dispatch = (event) => {
    let payload = {};
    try {
      payload = JSON.parse(event.data || "{}");
    } catch (error) {
      console.error("状态事件解析失败", error);
      return;
    }
    const numericEventId = Number(payload.id || event.lastEventId || 0);
    if (Number.isFinite(numericEventId) && numericEventId > lastEventId) {
      lastEventId = numericEventId;
    }
    const eventPayload = {
      ...payload,
      type: payload.type || event.type || "state.changed",
    };
    for (const subscriber of subscribers.values()) {
      subscriber(eventPayload);
    }
  };

  eventSource.addEventListener("state.changed", dispatch);
  eventSource.addEventListener("archive.completed", dispatch);
  eventSource.addEventListener("archive.failed", dispatch);
  eventSource.addEventListener("organize.completed", dispatch);
  eventSource.addEventListener("archive_model_index_rebuild.changed", dispatch);
  eventSource.addEventListener("system_update.changed", dispatch);

  eventSource.onerror = () => {
    closeStateEventSource();
    if (subscribers.size && !reconnectTimer) {
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = 0;
        ensureStateEventSource();
      }, RECONNECT_DELAY_MS);
    }
  };
}

function closeStateEventSource() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

export function subscribeStateEvents(handler) {
  if (typeof handler !== "function") {
    return () => {};
  }
  subscriberId += 1;
  subscribers.set(subscriberId, handler);
  ensureStateEventSource();

  return () => {
    subscribers.delete(subscriberId);
    if (!subscribers.size) {
      closeStateEventSource();
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = 0;
      }
    }
  };
}

export function subscribeStateRefresh(scopes, callback, options = {}) {
  const scheduler = createScopedRefreshScheduler({
    scopes,
    types: options.types,
    callback,
    debounceMs: Number.isFinite(Number(options.debounceMs))
      ? Number(options.debounceMs)
      : DEFAULT_STATE_REFRESH_DEBOUNCE_MS,
    isHidden: () => typeof document !== "undefined" && document.hidden,
    setTimeoutFn: (callbackFn, delay) => window.setTimeout(callbackFn, delay),
    clearTimeoutFn: (timerId) => window.clearTimeout(timerId),
  });
  const onVisibilityChange = () => scheduler.handleVisibilityChange();

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  const unsubscribe = subscribeStateEvents((event) => scheduler.handleEvent(event));

  return () => {
    unsubscribe();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    }
    scheduler.dispose();
  };
}
