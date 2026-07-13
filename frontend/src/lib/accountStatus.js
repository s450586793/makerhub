export function accountOperationalView(operational = {}) {
  const tone = String(operational?.tone || "neutral").trim();
  return {
    label: String(operational?.label || "状态待确认").trim(),
    statusClass: tone === "danger" ? "is-expired" : tone === "warning" ? "is-warning" : "",
    message: String(operational?.message || "账号下载状态待确认，请测试。").trim(),
    action: String(operational?.action || "test").trim(),
  };
}
