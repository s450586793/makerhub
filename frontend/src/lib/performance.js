const API_SLOW_THRESHOLD_MS = 800;
const PAGE_SLOW_THRESHOLD_MS = 1200;
const MAX_DURATION_MS = 600000;
const MAX_RESPONSE_BYTES = 50 * 1024 * 1024;
const MAX_STATUS = 999;

let apiCount = 0;
let slowApiCount = 0;
let maxApiDurationMs = 0;
let apiMetrics = [];

function nowMs() {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function boundedNumber(value, maximum = MAX_DURATION_MS) {
  return Math.round(Math.min(safeNumber(value), maximum) * 10) / 10;
}

function boundedInteger(value, maximum) {
  return Math.round(Math.min(safeNumber(value), maximum));
}

function safeRoute(value) {
  return String(value || "").split(/[?#]/, 1)[0].slice(0, 240);
}

function maxMetric(metrics, key) {
  return Math.max(0, ...metrics.map((metric) => safeNumber(metric[key])));
}

function currentSnapshot() {
  return {
    apiCount,
    slowApiCount,
    maxApiDurationMs,
    apiIndex: apiMetrics.length,
  };
}

export function resetPerformanceStateForTests() {
  apiCount = 0;
  slowApiCount = 0;
  maxApiDurationMs = 0;
  apiMetrics = [];
}

export function recordApiDuration(path, durationMs) {
  const duration = boundedNumber(durationMs);
  recordApiRequestMetrics(path, {
    ttfbMs: duration,
    totalMs: duration,
  });
}

export function recordApiRequestMetrics(path, metrics = {}) {
  const cleanPath = String(path || "").split("?", 1)[0];
  if (!cleanPath.startsWith("/api/") || cleanPath === "/api/performance/events") {
    return;
  }

  const totalMs = boundedNumber(metrics.totalMs);
  const metric = {
    ttfb_ms: boundedNumber(metrics.ttfbMs),
    body_parse_ms: boundedNumber(metrics.bodyParseMs),
    total_ms: totalMs,
    response_bytes: boundedInteger(metrics.responseBytes, MAX_RESPONSE_BYTES),
    status: boundedInteger(metrics.status, MAX_STATUS),
  };
  apiCount += 1;
  if (totalMs >= API_SLOW_THRESHOLD_MS) {
    slowApiCount += 1;
  }
  maxApiDurationMs = Math.max(maxApiDurationMs, totalMs);
  apiMetrics.push(metric);
}

export function getApiPerformanceMetricsForTests() {
  return apiMetrics.map((metric) => ({ ...metric }));
}

export function normalizePagePerformancePayload({
  page,
  route,
  durationMs,
  dataReadyMs = durationMs,
  enrichmentReadyMs = 0,
  eventKind = "page",
  since = null,
}) {
  const base = since || { apiCount: 0, slowApiCount: 0, apiIndex: 0 };
  const count = Math.max(0, apiCount - safeNumber(base.apiCount));
  const slowCount = Math.max(0, slowApiCount - safeNumber(base.slowApiCount));
  const startIndex = Math.max(0, Math.min(apiMetrics.length, Math.floor(safeNumber(base.apiIndex))));
  const scopedMetrics = since ? apiMetrics.slice(startIndex) : apiMetrics;
  const maxDuration = since ? maxMetric(scopedMetrics, "total_ms") : maxApiDurationMs;
  return {
    page: String(page || "unknown").slice(0, 80),
    route: safeRoute(route),
    event_kind: eventKind === "enrichment" ? "enrichment" : "page",
    duration_ms: boundedNumber(durationMs),
    data_ready_ms: boundedNumber(dataReadyMs),
    enrichment_ready_ms: boundedNumber(enrichmentReadyMs),
    api_count: count,
    slow_api_count: slowCount,
    max_api_duration_ms: boundedNumber(maxDuration),
    max_ttfb_ms: boundedNumber(maxMetric(scopedMetrics, "ttfb_ms")),
    max_parse_ms: boundedNumber(maxMetric(scopedMetrics, "body_parse_ms")),
    max_total_ms: boundedNumber(maxMetric(scopedMetrics, "total_ms")),
  };
}

export function createPagePerformanceTracker(options = {}) {
  const {
    page = "unknown",
    route = () => (typeof window === "undefined" ? "" : `${window.location.pathname}${window.location.search}`),
    eventKind = "page",
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
  let dataReadyMs = null;
  let enrichmentReadyMs = null;

  function elapsedMs() {
    return boundedNumber(now() - startedAt);
  }

  return {
    markDataReady() {
      if (dataReadyMs === null) {
        dataReadyMs = elapsedMs();
      }
      return dataReadyMs;
    },
    markEnrichmentReady() {
      if (enrichmentReadyMs === null) {
        enrichmentReadyMs = elapsedMs();
      }
      return enrichmentReadyMs;
    },
    async finish() {
      const durationMs = dataReadyMs ?? elapsedMs();
      const milestoneMs = Math.max(durationMs, enrichmentReadyMs ?? 0);
      if (milestoneMs < PAGE_SLOW_THRESHOLD_MS) {
        return null;
      }
      const payload = normalizePagePerformancePayload({
        page,
        route: typeof route === "function" ? route() : route,
        durationMs,
        dataReadyMs: dataReadyMs ?? (eventKind === "enrichment" ? 0 : durationMs),
        enrichmentReadyMs: enrichmentReadyMs ?? 0,
        eventKind,
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
