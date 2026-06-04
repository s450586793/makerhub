export function dashboardStatusElementKind(_item) {
  return "div";
}

const RUNTIME_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  waiting_children: "等待子任务",
  paused: "已暂停",
  blocked: "需处理",
  failed: "失败",
  completed: "已完成",
};

const BLOCKED_REASON_LABELS = {
  needs_cookie: "需要 Cookie",
  needs_verification: "需要验证",
  rate_limited: "访问受限",
  source_unavailable: "源端不可用",
  worker_stopped: "Worker 未运行",
};

export function dashboardStatusActions(item = {}) {
  const actions = [];
  if (item?.route && item?.action_label) {
    actions.push({
      kind: "route",
      label: item.action_label,
      to: item.route,
    });
  }
  if (item?.url && item?.action_label) {
    actions.push({
      kind: "external",
      label: item.action_label,
      href: item.url,
    });
  }
  return actions;
}

export function dashboardStatusAction(item) {
  return dashboardStatusActions(item)[0] || null;
}

export function shouldShowDashboardStatusDetail(item = {}) {
  if (!item?.detail) {
    return false;
  }
  return !(Array.isArray(item?.checks) && item.checks.length > 0);
}

export function normalizeRuntimeStatusLabel(status, blockedReason = "") {
  const cleanStatus = String(status || "").trim().toLowerCase();
  const cleanReason = String(blockedReason || "").trim().toLowerCase();
  if (cleanStatus === "blocked" && BLOCKED_REASON_LABELS[cleanReason]) {
    return BLOCKED_REASON_LABELS[cleanReason];
  }
  return RUNTIME_STATUS_LABELS[cleanStatus] || "未知";
}

export function runtimeTaskAction(item = {}) {
  const status = String(item.status || "").trim().toLowerCase();
  const reason = String(item.blocked_reason || item.blockedReason || "").trim().toLowerCase();
  if (item.stale || item.recoverable) {
    return {
      kind: "api",
      label: "修复队列",
      endpoint: "/api/tasks/archive-queue/repair",
      method: "POST",
    };
  }
  if (status === "blocked" && ["needs_verification", "needs_cookie"].includes(reason) && item.url) {
    return {
      kind: "external",
      label: "访问主页",
      href: item.url,
    };
  }
  return null;
}
