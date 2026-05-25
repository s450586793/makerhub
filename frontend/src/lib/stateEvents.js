const EVENT_SOURCE_URL = "/api/events/state";
const RECONNECT_DELAY_MS = 5000;
const DEFAULT_DEBOUNCE_MS = 450;

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
  eventSource.addEventListener("profile_backfill.changed", dispatch);
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
  const scopeSet = new Set((Array.isArray(scopes) ? scopes : [scopes]).filter(Boolean));
  const eventTypes = new Set(Array.isArray(options.types) ? options.types.filter(Boolean) : []);
  const debounceMs = Number.isFinite(Number(options.debounceMs)) ? Number(options.debounceMs) : DEFAULT_DEBOUNCE_MS;
  let timer = 0;
  let pendingWhenVisible = false;

  const invoke = (event) => {
    if (typeof callback !== "function") {
      return;
    }
    if (typeof document !== "undefined" && document.hidden) {
      pendingWhenVisible = true;
      return;
    }
    if (timer) {
      window.clearTimeout(timer);
    }
    timer = window.setTimeout(() => {
      timer = 0;
      callback(event);
    }, debounceMs);
  };

  const onVisibilityChange = () => {
    if (typeof document !== "undefined" && document.hidden) {
      return;
    }
    if (!pendingWhenVisible) {
      return;
    }
    pendingWhenVisible = false;
    invoke({ type: "visibility.resumed", scope: "visibility" });
  };

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  const unsubscribe = subscribeStateEvents((event) => {
    const scope = event?.scope || event?.payload?.scope || "";
    const type = event?.type || "";
    if (scopeSet.size && !scopeSet.has(scope) && !scopeSet.has("*")) {
      return;
    }
    if (eventTypes.size && !eventTypes.has(type)) {
      return;
    }
    invoke(event);
  });

  return () => {
    unsubscribe();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    }
    if (timer) {
      window.clearTimeout(timer);
      timer = 0;
    }
  };
}
