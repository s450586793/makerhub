const ACTIVE_STATUSES = new Set(["queued", "launching_helper", "running", "pending_startup"]);

const PHASE_PROGRESS = {
  queued: { label: "已提交更新", progress: 5 },
  launching_helper: { label: "启动更新 helper", progress: 10 },
  pulling: { label: "正在拉取镜像", progress: 25 },
  updating_web: { label: "正在更新 Web", progress: 30 },
  pulling_web: { label: "正在拉取 Web 镜像", progress: 35 },
  creating_web: { label: "正在创建 Web 容器", progress: 40 },
  switching_web: { label: "正在切换 Web 容器", progress: 48 },
  starting_web: { label: "正在启动 Web 容器", progress: 55 },
  web_updated: { label: "Web 容器已更新", progress: 58 },
  updating_worker: { label: "正在更新 Worker", progress: 60 },
  pulling_worker: { label: "正在拉取 Worker 镜像", progress: 62 },
  creating_worker: { label: "正在创建 Worker 容器", progress: 68 },
  switching_worker: { label: "正在切换 Worker 容器", progress: 74 },
  starting_worker: { label: "正在启动 Worker 容器", progress: 80 },
  worker_updated: { label: "Worker 容器已更新", progress: 84 },
  recreating: { label: "正在替换 App 容器", progress: 88 },
  switching: { label: "正在切换 App 容器", progress: 92 },
  starting: { label: "等待服务恢复", progress: 96 },
  completed: { label: "更新完成", progress: 100 },
  version_mismatch: { label: "版本校验失败", progress: 96 },
  failed: { label: "更新失败", progress: 0 },
};

function cleanText(value) {
  return String(value || "").trim();
}

function clampProgress(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

export function systemUpdateProgressState(update = {}) {
  const status = cleanText(update.status || "idle");
  const phase = cleanText(update.phase || status || "idle");
  const phaseState = PHASE_PROGRESS[phase] || null;
  const active = ACTIVE_STATUSES.has(status);
  const completed = status === "succeeded";
  const failed = status === "failed";
  const hasMessage = Boolean(cleanText(update.message) || cleanText(update.last_error));
  const visible = active || completed || failed || hasMessage;

  if (!visible) {
    return {
      visible: false,
      active: false,
      completed: false,
      failed: false,
      label: "",
      message: "",
      progress: 0,
      percentText: "0%",
      variant: "idle",
    };
  }

  const fallbackProgress = active ? 50 : 0;
  const progress = completed ? 100 : clampProgress(phaseState?.progress ?? fallbackProgress);
  const label = completed ? "更新完成" : phaseState?.label || (failed ? "更新失败" : "正在更新");
  const message = cleanText(failed ? update.last_error || update.message : update.message) || label;
  const variant = failed ? "failed" : completed ? "success" : active ? "running" : "idle";

  return {
    visible: true,
    active,
    completed,
    failed,
    label,
    message,
    progress,
    percentText: `${progress}%`,
    variant,
  };
}
