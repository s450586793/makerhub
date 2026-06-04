import { storyboardSegments, VIDEO_DURATION_SECONDS } from "./storyboard.mjs";

export const CAPTURE_VIEWPORT = { width: 1920, height: 1080, deviceScaleFactor: 1 };
export const CAPTURE_TIMEOUT_MS = 30000;
export const CAPTURE_SETTLE_MS = 1200;

export const captureTargets = [
  {
    order: 1,
    id: "model-library",
    route: "/models",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 2,
    id: "dashboard",
    route: "/",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 3,
    id: "online-sync",
    route: "/settings?tab=accounts",
    waitFor: ".online-account-card, .settings-panel",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 4,
    id: "subscriptions",
    route: "/subscriptions",
    waitFor: ".source-library-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 5,
    id: "remote-refresh",
    route: "/remote-refresh",
    waitFor: ".remote-refresh-layout, .remote-refresh-card",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 6,
    id: "source-deleted",
    route: "/models?tag=__source_deleted__",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
  {
    order: 7,
    id: "local-upload",
    route: "/organizer",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [
      { type: "click", name: "open-local-import", selector: "button:has-text('导入本地模型')", optional: true },
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 8,
    id: "sharing",
    route: "/models",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [
      { type: "click", name: "enter-select-mode", selector: "button:has-text('选择')", optional: true },
      { type: "click", name: "select-first-card", selector: ".gallery-card__select-toggle", optional: true },
      { type: "click", name: "open-share-dialog", selector: "button:has-text('分享')", optional: true },
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 9,
    id: "verification",
    route: "/",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [{ type: "wait", ms: CAPTURE_SETTLE_MS }],
  },
];

export function targetById(id) {
  return captureTargets.find((target) => target.id === id) || null;
}

export function safeCaptureFilename(id) {
  const target = targetById(id);
  if (!target) {
    throw new Error(`Unknown capture target: ${id}`);
  }
  return `${String(target.order).padStart(2, "0")}-${target.id}.png`;
}

export function resolveCaptureUrl(baseUrl, route) {
  const normalizedBase = String(baseUrl || "").trim();
  if (!normalizedBase) {
    throw new Error("MAKERHUB_VIDEO_BASE_URL is required");
  }
  return new URL(route, normalizedBase.endsWith("/") ? normalizedBase : `${normalizedBase}/`).toString();
}

export function validateCapturePlan() {
  const errors = [];
  const storyboardIds = new Set(storyboardSegments.map((segment) => segment.id));
  const targetIds = new Set(captureTargets.map((target) => target.id));
  const finalSegment = storyboardSegments.at(-1);

  if (!finalSegment || finalSegment.start + finalSegment.duration !== VIDEO_DURATION_SECONDS) {
    errors.push("storyboard duration must equal VIDEO_DURATION_SECONDS");
  }

  for (const id of storyboardIds) {
    if (!targetIds.has(id)) {
      errors.push(`missing capture target for ${id}`);
    }
  }

  for (const target of captureTargets) {
    if (!storyboardIds.has(target.id)) {
      errors.push(`capture target has no storyboard segment: ${target.id}`);
    }
    if (!Number.isInteger(target.order) || target.order < 1) {
      errors.push(`capture target has invalid order: ${target.id}`);
    }
    if (!target.route.startsWith("/")) {
      errors.push(`capture target route must be relative: ${target.id}`);
    }
  }

  return { valid: errors.length === 0, errors };
}
