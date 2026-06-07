const API_SLOW_THRESHOLD_MS = 800;
const PAGE_SLOW_THRESHOLD_MS = 1200;

let apiCount = 0;
let slowApiCount = 0;
let maxApiDurationMs = 0;
let apiDurations = [];

function nowMs() {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function currentSnapshot() {
  return {
    apiCount,
    slowApiCount,
    maxApiDurationMs,
    apiIndex: apiDurations.length,
  };
}

export function resetPerformanceStateForTests() {
  apiCount = 0;
  slowApiCount = 0;
  maxApiDurationMs = 0;
  apiDurations = [];
}

export function recordApiDuration(path, durationMs) {
  const cleanPath = String(path || "").split("?", 1)[0];
  if (!cleanPath.startsWith("/api/") || cleanPath === "/api/performance/events") {
    return;
  }

  const duration = safeNumber(durationMs);
  apiCount += 1;
  if (duration >= API_SLOW_THRESHOLD_MS) {
    slowApiCount += 1;
  }
  maxApiDurationMs = Math.max(maxApiDurationMs, duration);
  apiDurations.push(duration);
}

export function normalizePagePerformancePayload({ page, route, durationMs, since = null }) {
  const base = since || { apiCount: 0, slowApiCount: 0, apiIndex: 0 };
  const count = Math.max(0, apiCount - safeNumber(base.apiCount));
  const slowCount = Math.max(0, slowApiCount - safeNumber(base.slowApiCount));
  const startIndex = Math.max(0, Math.min(apiDurations.length, Math.floor(safeNumber(base.apiIndex))));
  const maxDuration = since
    ? Math.max(0, ...apiDurations.slice(startIndex))
    : maxApiDurationMs;
  return {
    page: String(page || "unknown").slice(0, 80),
    route: String(route || "").slice(0, 240),
    duration_ms: Math.round(safeNumber(durationMs) * 10) / 10,
    api_count: count,
    slow_api_count: slowCount,
    max_api_duration_ms: Math.round(maxDuration * 10) / 10,
  };
}

export function createPagePerformanceTracker(options = {}) {
  const {
    page = "unknown",
    route = () => (typeof window === "undefined" ? "" : `${window.location.pathname}${window.location.search}`),
    now = nowMs,
    request = async (path, requestOptions) => {
      const response = await fetch(path, {
        method: requestOptions.method || "GET",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(requestOptions.body || {}),
        credentials: "include",
        cache: "no-store",
      });
      return response.ok ? response : null;
    },
  } = options;
  const startedAt = now();
  const startedSnapshot = currentSnapshot();

  return {
    async finish() {
      const durationMs = now() - startedAt;
      if (durationMs < PAGE_SLOW_THRESHOLD_MS) {
        return null;
      }
      const payload = normalizePagePerformancePayload({
        page,
        route: typeof route === "function" ? route() : route,
        durationMs,
        since: startedSnapshot,
      });
      try {
        await request("/api/performance/events", {
          method: "POST",
          body: payload,
          redirectOn401: false,
        });
      } catch {
        return null;
      }
      return payload;
    },
  };
}
