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

function normalizeScopes(scopes) {
  const values = Array.isArray(scopes) ? scopes : [scopes];
  return [...new Set(values.map((scope) => String(scope || "").trim()).filter(Boolean))].sort();
}

function subscribedScopes() {
  const scopes = new Set();
  for (const subscriber of subscribers.values()) {
    for (const scope of subscriber.scopes) {
      scopes.add(scope);
    }
  }
  return scopes.has("*") ? [] : [...scopes].sort();
}

function rebuildStateEventSource() {
  if (!eventSource) {
    return;
  }
  closeStateEventSource();
  ensureStateEventSource();
}

function ensureStateEventSource() {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    return;
  }
  if (eventSource || !subscribers.size) {
    return;
  }

  const query = new URLSearchParams();
  if (lastEventId > 0) {
    query.set("last_event_id", String(lastEventId));
  }
  for (const scope of subscribedScopes()) {
    query.append("scope", scope);
  }
  const eventUrl = query.size ? `${EVENT_SOURCE_URL}?${query.toString()}` : EVENT_SOURCE_URL;
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
      subscriber.handler(eventPayload);
    }
  };

  eventSource.addEventListener("state.changed", dispatch);
  eventSource.addEventListener("archive.completed", dispatch);
  eventSource.addEventListener("archive.failed", dispatch);
  eventSource.addEventListener("organize.completed", dispatch);
  eventSource.addEventListener("source_library.changed", dispatch);
  eventSource.addEventListener("archive_model_index_rebuild.changed", dispatch);
  eventSource.addEventListener("account_health.changed", dispatch);
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

export function subscribeStateEvents(handler, scopes = []) {
  if (typeof handler !== "function") {
    return () => {};
  }
  subscriberId += 1;
  subscribers.set(subscriberId, { handler, scopes: normalizeScopes(scopes) });
  rebuildStateEventSource();
  ensureStateEventSource();

  return () => {
    subscribers.delete(subscriberId);
    rebuildStateEventSource();
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
    eventRules: options.eventRules,
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

  const unsubscribe = subscribeStateEvents((event) => scheduler.handleEvent(event), scopes);

  return () => {
    unsubscribe();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    }
    scheduler.dispose();
  };
}
