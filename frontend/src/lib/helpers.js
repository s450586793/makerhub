export function avatarText(displayName, username = "U") {
  const source = String(displayName || "").trim() || String(username || "").trim() || "U";
  return source.slice(0, 1).toUpperCase();
}

export function parseServerDate(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return null;
  }

  const normalized = raw.includes(" ") ? raw.replace(" ", "T") : raw;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
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
