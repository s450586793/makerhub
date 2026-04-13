export function avatarText(displayName, username = "U") {
  const source = String(displayName || "").trim() || String(username || "").trim() || "U";
  return source.slice(0, 1).toUpperCase();
}


export function safeNextPath(value) {
  const candidate = String(value || "").trim();
  return candidate.startsWith("/") ? candidate : "/";
}


export function formatDate(value) {
  return String(value || "").trim() || "未知时间";
}


export function encodeModelPath(modelDir) {
  return `/models/${encodeURI(String(modelDir || "").replace(/^\/+/, ""))}`;
}
