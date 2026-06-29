const RUN_LABELS = {
  queued: "排队中",
  discovering: "发现中",
  planned: "已规划",
  running: "运行中",
  paused: "已暂停",
  blocked: "需处理",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "可恢复",
};

const FAILURE_LABELS = {
  failed: "失败",
  skipped: "已跳过",
  missing_3mf: "缺失 3MF",
  verification_required: "需要验证",
  cookie_invalid: "Cookie 异常",
  daily_limit: "每日上限",
  network_error: "网络异常",
  not_found: "源端无文件",
};

export function runtimeRunLabel(status = "") {
  return RUN_LABELS[String(status || "").trim().toLowerCase()] || "未知";
}

export function runtimeFailureLabel(status = "") {
  return FAILURE_LABELS[String(status || "").trim().toLowerCase()] || "失败";
}

export function runtimeTaskShape(payload = {}) {
  const runtime = payload?.runtime;
  if (runtime && Array.isArray(runtime.runs) && Array.isArray(runtime.batches)) {
    const failures = Array.isArray(runtime.failures) ? runtime.failures : [];
    const hasRuntimeItems = runtime.runs.length > 0 || runtime.batches.length > 0 || failures.length > 0;
    if (!hasRuntimeItems) {
      return {
        mode: "legacy",
        legacy: payload,
      };
    }
    return {
      mode: "runtime",
      runs: runtime.runs,
      batches: runtime.batches,
      failures,
    };
  }
  return {
    mode: "legacy",
    legacy: payload,
  };
}
