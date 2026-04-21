const CHINA_TIMEZONE = "Asia/Shanghai";

export function avatarText(displayName, username = "U") {
  const source = String(displayName || "").trim() || String(username || "").trim() || "U";
  return source.slice(0, 1).toUpperCase();
}

export function parseServerDate(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return null;
  }

  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(raw);
  const hasDateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
  let normalized = raw.includes(" ") ? raw.replace(" ", "T") : raw;
  if (hasDateOnly) {
    normalized = `${normalized}T00:00:00`;
  }
  if (!hasTimezone) {
    normalized = `${normalized}+08:00`;
  }
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatServerDateTime(value, options = {}) {
  const {
    fallback = "-",
    second = false,
  } = options;
  const date = parseServerDate(value);
  if (!date) {
    return fallback;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: CHINA_TIMEZONE,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(second ? { second: "2-digit" } : {}),
  }).format(date);
}

export function safeNextPath(value) {
  const candidate = String(value || "").trim();
  return candidate.startsWith("/") ? candidate : "/";
}


export function formatDate(value) {
  return String(value || "").trim() || "未知时间";
}


export function encodeModelPath(modelDir) {
  const value = String(modelDir || "").replace(/^\/+/, "");
  const encoded = value
    .split("/")
    .filter((segment) => segment.length > 0)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `/models/${encoded}`;
}
