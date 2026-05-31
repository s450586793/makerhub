export const DEFAULT_FRAME_ASPECT_RATIO = "520 / 640";
export const DRAG_MOVE_INTERVAL_MS = 50;
export const HOVER_MOVE_INTERVAL_MS = 180;
export const DRAG_THRESHOLD_PX = 4;

function numberOrFallback(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function mapFramePointToViewport(event, rect, viewport = {}) {
  const width = numberOrFallback(rect?.width, 0);
  const height = numberOrFallback(rect?.height, 0);
  if (!rect || !width || !height) {
    return { x: 0, y: 0 };
  }
  const viewportWidth = numberOrFallback(viewport?.width, 1365);
  const viewportHeight = numberOrFallback(viewport?.height, 768);
  const scaleX = viewportWidth / width;
  const scaleY = viewportHeight / height;
  return {
    x: Math.max(0, Math.round((Number(event?.clientX || 0) - Number(rect.left || 0)) * scaleX)),
    y: Math.max(0, Math.round((Number(event?.clientY || 0) - Number(rect.top || 0)) * scaleY)),
  };
}

export function frameAspectRatio(viewport = {}) {
  const width = Number(viewport?.width || 0);
  const height = Number(viewport?.height || 0);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return DEFAULT_FRAME_ASPECT_RATIO;
  }
  return `${Math.round(width)} / ${Math.round(height)}`;
}

export function createDragState({ pointerId, x, y, now = Date.now() }) {
  return {
    pointerId,
    startX: Number(x || 0),
    startY: Number(y || 0),
    lastX: Number(x || 0),
    lastY: Number(y || 0),
    lastSentAt: Number(now || 0),
    dragged: false,
  };
}

export function notePointerMove(state, { pointerId, x, y, now = Date.now() }) {
  if (!state || state.pointerId !== pointerId) {
    return state;
  }
  const nextX = Number(x || 0);
  const nextY = Number(y || 0);
  const dx = nextX - state.startX;
  const dy = nextY - state.startY;
  state.lastX = nextX;
  state.lastY = nextY;
  if (Math.sqrt(dx * dx + dy * dy) >= DRAG_THRESHOLD_PX) {
    state.dragged = true;
  }
  state.lastMoveAt = Number(now || 0);
  return state;
}

export function shouldSendPointerMove({ state, pointerId, now = Date.now(), dragging = false }) {
  if (!state || state.pointerId !== pointerId) {
    return false;
  }
  const interval = dragging ? DRAG_MOVE_INTERVAL_MS : HOVER_MOVE_INTERVAL_MS;
  return Number(now || 0) - Number(state.lastSentAt || 0) >= interval;
}

export function markPointerMoveSent(state, now = Date.now()) {
  if (state) {
    state.lastSentAt = Number(now || 0);
  }
  return state;
}

export function isDragClickSuppressed(state) {
  return Boolean(state?.dragged);
}
