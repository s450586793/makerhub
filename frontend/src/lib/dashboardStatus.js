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

export function sourceRefreshStatusLabel(status, running) {
  if (running || status === "running") {
    return "刷新中";
  }
  const normalized = String(status || "").trim();
  const mapping = {
    idle: "空闲",
    resuming: "恢复中",
    deferred: "已延后",
    interrupted: "可恢复",
    disabled: "已停用",
    error: "异常",
  };
  return mapping[normalized] || "空闲";
}

export function sourceRefreshActiveRun(automationItem = {}) {
  const activeRun = automationItem?.source_refresh?.runs?.active_run;
  if (!activeRun || typeof activeRun !== "object") {
    return {};
  }
  const status = String(activeRun.status || "").trim();
  return activeRun.run_id && ["queued", "running", "resuming", "paused", "interrupted"].includes(status) ? activeRun : {};
}

export function legacyRemoteRefreshActiveRun(item = {}) {
  const activeRun = item?.active_run;
  if (!activeRun || typeof activeRun !== "object") {
    return {};
  }
  const status = String(activeRun.status || "").trim();
  return activeRun.batch_id && ["running", "resuming", "interrupted"].includes(status) ? activeRun : {};
}

function remoteRefreshItem(automationOrRemote = {}) {
  return automationOrRemote?.remote_refresh || automationOrRemote || {};
}

export function getSourceRefreshDisplayTotals(automationOrRemote = {}) {
  const activeRun = sourceRefreshActiveRun(automationOrRemote);
  if (activeRun.run_id) {
    return {
      total: Number(activeRun.candidate_total || 0),
      completed: Number(activeRun.completed_total || 0),
      remaining: Number(activeRun.remaining_total || 0),
      source: "source_refresh",
    };
  }

  const item = remoteRefreshItem(automationOrRemote);
  const legacyRun = legacyRemoteRefreshActiveRun(item);
  if (legacyRun.batch_id) {
    return {
      total: Number(legacyRun.candidate_total || 0),
      completed: Number(legacyRun.completed_total || 0),
      remaining: Number(legacyRun.remaining_total || 0),
      source: "remote_refresh_active",
    };
  }

  return {
    total: Number(item?.last_batch_total || 0),
    completed: Number(item?.last_batch_succeeded || 0)
      + Number(item?.last_batch_failed || 0)
      + Number(item?.last_batch_skipped || 0),
    remaining: Number(item?.last_remaining_total || 0),
    source: "remote_refresh_summary",
  };
}

export function sourceRefreshPillClass(item = {}) {
  if (item?.running || item?.status === "running" || item?.status === "resuming") {
    return "count-pill--warn";
  }
  if (item?.status === "error") {
    return "count-pill--danger";
  }
  if (item?.status === "interrupted" || item?.status === "deferred") {
    return "count-pill--warn";
  }
  if (item?.enabled) {
    return "count-pill--ok";
  }
  return "";
}

export function sourceRefreshDeferText(item = {}, formatDateTime = (value) => value || "未安排") {
  const reason = String(item?.last_defer_reason || "").trim();
  if (!reason) {
    return "无";
  }
  const mapping = {
    archive_queue_busy: "归档队列占用",
    local_organizer_busy: "本地整理占用",
    stale_runtime_state: "队列状态待修复",
  };
  const reasonText = mapping[reason] || reason;
  const timeText = formatDateTime(item?.last_deferred_at);
  return `${reasonText} · ${timeText}`;
}
