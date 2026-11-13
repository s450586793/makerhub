import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { performance } from "node:perf_hooks";


export const MAX_PROVIDER_ATTEMPTS = 2;
export const AUTO_VERIFY_TIMEOUT_MS = 50_000;
export const VISION_TIMEOUT_MS = 8_000;

const MAX_VISION_OUTPUT_BYTES = 64 * 1024;
const CLEANUP_TIMEOUT_MS = 1_000;
const DEFAULT_TURNSTILE_WAIT_MS = 500;
const POLL_INTERVAL_MS = 100;

const SAFE_REASON_CODES = new Set([
  "aborted",
  "ambiguous_candidates",
  "ambiguous_gap",
  "attempts_exhausted",
  "candidate_count_invalid",
  "challenge_changed",
  "challenge_unchanged",
  "challenge_unsupported",
  "checkbox_unavailable",
  "cleanup_failed",
  "click_layout_invalid",
  "click_target_unavailable",
  "completed",
  "confirmation_unavailable",
  "confidence_too_low",
  "coordinate_ambiguous",
  "coordinate_invalid",
  "coordinate_not_found",
  "discovery_failed",
  "distance_invalid",
  "empty_screenshot",
  "gap_not_found",
  "geometry_invalid",
  "image_base64_invalid",
  "image_decode_failed",
  "image_dimensions_invalid",
  "image_depth_invalid",
  "image_foreground_missing",
  "image_format_invalid",
  "image_size_invalid",
  "image_width_invalid",
  "input_too_large",
  "interaction_failed",
  "interaction_disabled",
  "json_invalid",
  "low_confidence",
  "mode_invalid",
  "no_challenge",
  "outcome_failed",
  "payload_invalid",
  "piece_restore_failed",
  "piece_unavailable",
  "request_invalid",
  "slider_geometry_invalid",
  "solved",
  "target_count_invalid",
  "target_segmentation_failed",
  "timeout",
  "trajectory_out_of_bounds",
  "unknown",
  "unsupported_fields",
  "verification_failed",
  "vision_rejected",
]);

const GEETEST_CONTAINERS = ".geetest_box, .geetest_panel, [class*='geetest']";
const CLICK_TARGET_SELECTORS = [
  ".geetest_ques_tips img",
  ".geetest_tip_img",
  ".geetest_ques_back img",
  ".geetest_question img",
  "[class*='geetest'][class*='target'] img",
];
const CLICK_CANDIDATE_SELECTORS = [
  ".geetest_item img",
  ".geetest_item",
  ".geetest_image_item",
  ".geetest_icon_item",
  "[class*='geetest_item']",
  "[class*='candidate']",
];
const COORDINATE_TARGET_SELECTORS = [
  ".geetest_ques_tips",
  ".geetest_ques_back",
  ".geetest_question",
  "[class*='geetest'][class*='ques']",
];
const COORDINATE_BACKGROUND_SELECTORS = [
  ".geetest_bg",
  ".geetest_canvas_bg",
  ".geetest_window",
  "[class*='geetest'][class*='bg']",
];
const COORDINATE_CONFIRM_SELECTORS = [
  ".geetest_commit_tip",
  ".geetest_submit",
  "[class*='geetest'][class*='commit']",
  "[class*='geetest'][class*='submit']",
];
const SLIDER_HANDLE_SELECTORS = [
  ".geetest_slider_button",
  ".geetest_btn",
  "[class*='slider_button']",
  "[class*='slider'][class*='handle']",
  "[role='slider']",
  "[role='button'][class*='slider']",
];
const SLIDER_BACKGROUND_SELECTORS = [
  ".geetest_bg",
  ".geetest_canvas_bg",
  "[class*='geetest_bg']",
  "[class*='slider_bg']",
];
const SLIDER_PIECE_SELECTORS = [
  ".geetest_slice",
  ".geetest_canvas_slice",
  "[class*='geetest_slice']",
  "[class*='slider_piece']",
];
const TURNSTILE_RESPONSE_SELECTOR = "input[name='cf-turnstile-response']";
const TURNSTILE_IFRAME_SELECTOR = "iframe[src*='challenges.cloudflare.com']";
const TURNSTILE_CHECKBOX_SELECTORS = [
  "input[type='checkbox']",
  "[role='checkbox']",
  ".ctp-checkbox-label",
];

function clampRandom(random) {
  const value = Number(random());
  return Number.isFinite(value) ? Math.max(0, Math.min(value, 1)) : 0.5;
}

class VerificationError extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

function abortError(signal) {
  return signal?.reason instanceof Error
    ? signal.reason
    : new VerificationError("aborted");
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw abortError(signal);
}

function throwIfActionExpired(stage) {
  throwIfAborted(stage.signal);
  if (stage.remainingAction() <= 0) throw new VerificationError("timeout");
}

function abortable(promise, signal) {
  if (!signal) return Promise.resolve(promise);
  if (signal.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError(signal));
    signal.addEventListener("abort", onAbort, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => {
      signal.removeEventListener("abort", onAbort);
    });
  });
}

function cleanupOperation(promise, stage) {
  if (stage.remainingHard() <= 0 || stage.hardSignal.aborted) {
    Promise.resolve(promise).catch(() => undefined);
    return Promise.reject(new VerificationError("cleanup_failed"));
  }
  return abortable(promise, stage.hardSignal).catch((error) => {
    if (stage.hardSignal.aborted) throw new VerificationError("cleanup_failed");
    throw error;
  });
}

function createStageSignal(timeoutMs, externalSignal) {
  const actionController = new AbortController();
  const hardController = new AbortController();
  const startedAt = performance.now();
  const cleanupBudget = Math.min(
    CLEANUP_TIMEOUT_MS,
    Math.max(1, Math.floor(timeoutMs * 0.25)),
  );
  const actionDeadline = startedAt + Math.max(1, timeoutMs - cleanupBudget);
  const hardDeadline = startedAt + timeoutMs;
  const remaining = (deadline) => Math.max(0, deadline - performance.now());
  const onExternalAbort = () => {
    const reason = externalSignal.reason || new VerificationError("aborted");
    actionController.abort(reason);
  };
  if (externalSignal?.aborted) onExternalAbort();
  else externalSignal?.addEventListener("abort", onExternalAbort, { once: true });
  const actionTimer = setTimeout(() => {
    actionController.abort(new VerificationError("timeout"));
  }, remaining(actionDeadline));
  const hardTimer = setTimeout(() => {
    hardController.abort(new VerificationError("timeout"));
  }, remaining(hardDeadline));
  return {
    signal: actionController.signal,
    hardSignal: hardController.signal,
    actionDeadline,
    hardDeadline,
    remainingAction: () => remaining(actionDeadline),
    remainingHard: () => remaining(hardDeadline),
    cleanup() {
      clearTimeout(actionTimer);
      clearTimeout(hardTimer);
      externalSignal?.removeEventListener("abort", onExternalAbort);
    },
  };
}

export function buildDragTrajectory(distance, random = Math.random) {
  const target = Math.max(0, Number(distance) || 0);
  if (target === 0) return [{ x: 0, y: 0 }];

  const points = [];
  const approachPoints = 16;
  for (let index = 1; index <= approachPoints; index += 1) {
    const progress = index / (approachPoints + 2);
    const eased = progress * progress * (3 - (2 * progress));
    points.push({
      x: Math.min(target * 0.96, target * eased),
      y: Math.round((clampRandom(random) * 6) - 3),
    });
  }
  const overshoot = Math.min(6, Math.max(1, target * 0.02));
  points.push({ x: target + overshoot, y: Math.round((clampRandom(random) * 4) - 2) });
  points.push({ x: target + (overshoot * 0.35), y: Math.round((clampRandom(random) * 2) - 1) });
  points.push({ x: target, y: 0 });
  return points;
}

export function sanitizeVerificationResult(value = {}) {
  const provider = ["geetest4", "turnstile"].includes(value.provider) ? value.provider : "unknown";
  const challengeType = ["coordinate_click", "icon_click", "slider", "checkbox"].includes(value.challenge_type)
    ? value.challenge_type
    : "unknown";
  const rawAttempts = Number(value.attempts || 0);
  const attempts = Number.isFinite(rawAttempts)
    ? Math.max(0, Math.min(rawAttempts, MAX_PROVIDER_ATTEMPTS))
    : 0;
  const reason = typeof value.reason === "string" && SAFE_REASON_CODES.has(value.reason)
    ? value.reason
    : "unknown";
  return {
    attempted: Boolean(value.attempted),
    completed: Boolean(value.completed),
    provider,
    challenge_type: challengeType,
    attempts,
    reason,
    ...(Number.isFinite(Number(value.confidence)) ? { confidence: Number(value.confidence) } : {}),
  };
}

function cleanVisionPayload(payload) {
  if (String(payload?.mode || "").trim().toLowerCase() === "click") {
    return {
      mode: "click",
      target_png: payload?.target_png,
      candidate_pngs: Array.isArray(payload?.candidate_pngs) ? payload.candidate_pngs : [],
    };
  }
  if (String(payload?.mode || "").trim().toLowerCase() === "slider") {
    const geometry = payload?.geometry && typeof payload.geometry === "object" ? payload.geometry : {};
    return {
      mode: "slider",
      background_png: payload?.background_png,
      piece_png: payload?.piece_png,
      geometry: {
        image_width: geometry.image_width,
        image_height: geometry.image_height,
        track_width: geometry.track_width,
        track_height: geometry.track_height,
        handle_width: geometry.handle_width,
        piece_offset_x: geometry.piece_offset_x,
        piece_offset_y: geometry.piece_offset_y,
      },
    };
  }
  if (String(payload?.mode || "").trim().toLowerCase() === "coordinate_click") {
    return {
      mode: "coordinate_click",
      targets_png: payload?.targets_png,
      background_png: payload?.background_png,
    };
  }
  return { mode: "" };
}

export function runVisionRequest(payload, options = {}) {
  const spawnFn = options.spawnFn || spawn;
  const signal = options.signal;
  const requestedTimeout = Number(options.timeoutMs || VISION_TIMEOUT_MS);
  const timeoutMs = Math.max(1, Math.min(
    Number.isFinite(requestedTimeout) ? requestedTimeout : VISION_TIMEOUT_MS,
    VISION_TIMEOUT_MS,
  ));

  if (signal?.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnFn(
        process.env.PYTHON || "python",
        ["-m", "app.services.makerworld_captcha_vision"],
        { stdio: ["pipe", "pipe", "pipe"] },
      );
    } catch {
      reject(new Error("vision process unavailable"));
      return;
    }

    const stdout = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    let timer;
    const onAbort = () => fail("vision process aborted", true);
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    };
    const fail = (message, kill = false) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (kill && !child.killed) {
        try {
          child.kill("SIGKILL");
        } catch {}
      }
      reject(new Error(message));
    };
    const collect = (stream, chunks, chunk) => {
      if (settled) return;
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (stream === "stdout") stdoutBytes += buffer.length;
      else stderrBytes += buffer.length;
      if (stdoutBytes > MAX_VISION_OUTPUT_BYTES || stderrBytes > MAX_VISION_OUTPUT_BYTES) {
        fail("vision process output limit exceeded", true);
        return;
      }
      if (chunks) chunks.push(buffer);
    };
    timer = setTimeout(() => fail("vision process timed out", true), timeoutMs);

    child.stdout.on("data", (chunk) => collect("stdout", stdout, chunk));
    child.stderr.on("data", (chunk) => collect("stderr", null, chunk));
    child.on("error", () => fail("vision process unavailable"));
    child.stdin.on("error", () => fail("vision process input failed", true));
    child.on("close", (code) => {
      if (settled) return;
      if (code !== 0) {
        fail("vision process failed");
        return;
      }
      let result;
      try {
        result = JSON.parse(Buffer.concat(stdout).toString("utf8"));
      } catch {
        fail("vision process returned invalid JSON");
        return;
      }
      if (!result || typeof result !== "object" || Array.isArray(result)) {
        fail("vision process returned invalid JSON");
        return;
      }
      settled = true;
      cleanup();
      resolve(result);
    });

    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    try {
      child.stdin.end(JSON.stringify(cleanVisionPayload(payload)));
    } catch {
      fail("vision process input failed", true);
    }
  });
}

async function safeQuery(root, selector) {
  try {
    return await root?.$(selector);
  } catch {
    return null;
  }
}

async function safeQueryAll(root, selector) {
  try {
    return await root?.$$(selector) || [];
  } catch {
    return [];
  }
}

async function isVisible(handle) {
  if (!handle) return false;
  try {
    const bounds = await handle.boundingBox();
    if (!bounds || bounds.width <= 0 || bounds.height <= 0) return false;
    return await handle.evaluate((element) => {
      const style = window.getComputedStyle(element);
      return element.isConnected
        && style.display !== "none"
        && style.visibility !== "hidden"
        && Number(style.opacity || 1) > 0;
    });
  } catch {
    return false;
  }
}

async function elementValue(handle) {
  if (!handle) return "";
  try {
    return String(await handle.evaluate((element, action) => (
      action === "read-value" ? element.value || "" : ""
    ), "read-value") || "").trim();
  } catch {
    return "";
  }
}

async function firstVisible(root, selectors) {
  for (const selector of selectors) {
    const handle = await safeQuery(root, selector);
    if (await isVisible(handle)) return handle;
  }
  return null;
}

async function isPlausibleSliderHandle(handle, background) {
  try {
    const [handleBox, backgroundBox] = await Promise.all([
      handle.boundingBox(),
      background.boundingBox(),
    ]);
    if (!handleBox || !backgroundBox) return false;
    const handleCenterY = handleBox.y + (handleBox.height / 2);
    const maximumWidth = Math.min(96, backgroundBox.width * 0.4);
    return handleBox.width <= maximumWidth
      && handleBox.height <= Math.max(80, backgroundBox.height)
      && handleBox.x <= backgroundBox.x + (backgroundBox.width * 0.35)
      && handleBox.x + handleBox.width >= backgroundBox.x - (backgroundBox.width * 0.1)
      && handleCenterY >= backgroundBox.y - (backgroundBox.height * 0.25)
      && handleCenterY <= backgroundBox.y + (backgroundBox.height * 1.25);
  } catch {
    return false;
  }
}

async function visibleCandidates(root) {
  for (const selector of CLICK_CANDIDATE_SELECTORS) {
    const handles = await safeQueryAll(root, selector);
    const visible = [];
    for (const handle of handles) {
      if (await isVisible(handle)) visible.push(handle);
      if (visible.length > 6) break;
    }
    if (visible.length >= 2) return visible;
  }
  return [];
}

function validBox(box) {
  return box
    && [box.x, box.y, box.width, box.height].every((value) => Number.isFinite(Number(value)))
    && Number(box.width) > 0
    && Number(box.height) > 0;
}

function median(values) {
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function overlapArea(left, right) {
  const width = Math.max(0, Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x));
  const height = Math.max(0, Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y));
  return width * height;
}

async function validateClickLayout(challenge, signal) {
  const candidates = Array.isArray(challenge?.candidates) ? challenge.candidates : [];
  if (!challenge?.container || !challenge?.target || candidates.length < 2 || candidates.length > 6) {
    return false;
  }
  if (candidates.some((candidate) => !candidate || candidate === challenge.target)) return false;

  const handles = [challenge.container, challenge.target, ...candidates];
  let visible;
  let belongsToContainer;
  let sharesSelectionRegion;
  try {
    [visible, belongsToContainer, sharesSelectionRegion] = await Promise.all([
      Promise.all(handles.map((handle) => abortable(isVisible(handle), signal))),
      Promise.all([challenge.target, ...candidates].map((handle) => abortable(
        challenge.container.evaluate((container, action, element) => (
          action === "contains" && container.contains(element)
        ), "contains", handle),
        signal,
      ))),
      abortable(challenge.container.evaluate(
        (container, action, target, ...candidateElements) => {
          if (action !== "shared-selection-region" || candidateElements.length < 2) return false;
          if (
            !target?.isConnected
            || !container.contains(target)
            || candidateElements.some((candidate) => (
              !candidate?.isConnected
              || candidate === target
              || !container.contains(candidate)
            ))
          ) return false;

          const nearestSelectionRegion = (candidate) => {
            let ancestor = candidate.parentElement;
            while (ancestor && ancestor !== container) {
              let containedCandidates = 0;
              for (const sibling of candidateElements) {
                if (ancestor.contains(sibling)) containedCandidates += 1;
                if (containedCandidates >= 2) return ancestor;
              }
              ancestor = ancestor.parentElement;
            }
            return null;
          };
          const selectionRegion = nearestSelectionRegion(candidateElements[0]);
          return Boolean(
            selectionRegion
            && !selectionRegion.contains(target)
            && candidateElements.every((candidate) => (
              nearestSelectionRegion(candidate) === selectionRegion
            )),
          );
        },
        "shared-selection-region",
        challenge.target,
        ...candidates,
      ), signal),
    ]);
  } catch (error) {
    if (signal?.aborted) throw error;
    return false;
  }
  if (!visible.every(Boolean) || !belongsToContainer.every(Boolean) || !sharesSelectionRegion) {
    return false;
  }

  let boxes;
  try {
    boxes = await Promise.all(
      handles.map((handle) => abortable(handle.boundingBox(), signal)),
    );
  } catch (error) {
    if (signal?.aborted) throw error;
    return false;
  }
  if (!boxes.every(validBox)) return false;
  const [containerBox, targetBox, ...candidateBoxes] = boxes.map((box) => ({
    x: Number(box.x),
    y: Number(box.y),
    width: Number(box.width),
    height: Number(box.height),
  }));
  const containerTolerance = Math.max(4, Math.min(containerBox.width, containerBox.height) * 0.03);
  const withinContainer = (box) => (
    box.x >= containerBox.x - containerTolerance
    && box.y >= containerBox.y - containerTolerance
    && box.x + box.width <= containerBox.x + containerBox.width + containerTolerance
    && box.y + box.height <= containerBox.y + containerBox.height + containerTolerance
  );
  if (![targetBox, ...candidateBoxes].every(withinContainer)) return false;

  const medianWidth = median(candidateBoxes.map((box) => box.width));
  const medianHeight = median(candidateBoxes.map((box) => box.height));
  const widthTolerance = Math.max(6, medianWidth * 0.30);
  const heightTolerance = Math.max(6, medianHeight * 0.30);
  if (candidateBoxes.some((box) => (
    Math.abs(box.width - medianWidth) > widthTolerance
    || Math.abs(box.height - medianHeight) > heightTolerance
  ))) return false;

  for (let leftIndex = 0; leftIndex < candidateBoxes.length; leftIndex += 1) {
    const left = candidateBoxes[leftIndex];
    if (overlapArea(targetBox, left) > 0) return false;
    for (let rightIndex = leftIndex + 1; rightIndex < candidateBoxes.length; rightIndex += 1) {
      const right = candidateBoxes[rightIndex];
      const overlap = overlapArea(left, right);
      const smallerArea = Math.min(left.width * left.height, right.width * right.height);
      if (overlap > smallerArea * 0.05) return false;
    }
  }

  const selectionBox = {
    x: Math.min(...candidateBoxes.map((box) => box.x)),
    y: Math.min(...candidateBoxes.map((box) => box.y)),
    width: Math.max(...candidateBoxes.map((box) => box.x + box.width))
      - Math.min(...candidateBoxes.map((box) => box.x)),
    height: Math.max(...candidateBoxes.map((box) => box.y + box.height))
      - Math.min(...candidateBoxes.map((box) => box.y)),
  };
  if (overlapArea(targetBox, selectionBox) > 0) return false;

  const rowTolerance = Math.max(6, medianHeight * 0.35);
  const entries = candidateBoxes
    .map((box) => ({ box, centerX: box.x + (box.width / 2), centerY: box.y + (box.height / 2) }))
    .sort((left, right) => left.centerY - right.centerY || left.centerX - right.centerX);
  const rows = [];
  for (const entry of entries) {
    const row = rows.find((candidateRow) => Math.abs(entry.centerY - candidateRow.centerY) <= rowTolerance);
    if (row) {
      row.entries.push(entry);
      row.centerY = row.entries.reduce((total, item) => total + item.centerY, 0) / row.entries.length;
    } else {
      rows.push({ centerY: entry.centerY, entries: [entry] });
    }
  }
  if (rows.length > 3) return false;
  rows.sort((left, right) => left.centerY - right.centerY);
  const rowSizes = rows.map((row) => row.entries.length);
  if (rows.length > 1 && !rowSizes.every((size) => size === rowSizes[0])) return false;

  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    row.entries.sort((left, right) => left.centerX - right.centerX);
    if (row.entries.some((entry) => Math.abs(entry.centerY - row.centerY) > rowTolerance)) return false;
    const gaps = row.entries.slice(1).map((entry, gapIndex) => (
      entry.centerX - row.entries[gapIndex].centerX
    ));
    if (gaps.some((gap) => gap < medianWidth * 0.60 || gap > medianWidth * 3.50)) return false;
    if (gaps.length > 1 && Math.max(...gaps) > Math.min(...gaps) * 2.50) return false;
    if (index > 0) {
      const verticalGap = row.centerY - rows[index - 1].centerY;
      if (verticalGap < medianHeight * 0.50 || verticalGap > medianHeight * 3.50) return false;
    }
  }

  if (rows.length > 1) {
    const columnTolerance = Math.max(8, medianWidth * 0.60);
    for (let rowIndex = 1; rowIndex < rows.length; rowIndex += 1) {
      for (let columnIndex = 0; columnIndex < rows[0].entries.length; columnIndex += 1) {
        if (Math.abs(
          rows[rowIndex].entries[columnIndex].centerX - rows[0].entries[columnIndex].centerX,
        ) > columnTolerance) return false;
      }
    }
  }
  return true;
}

async function coordinateClickLayoutSnapshot(challenge, signal) {
  const handles = [
    challenge?.container,
    challenge?.targets,
    challenge?.background,
    challenge?.confirm,
  ];
  if (handles.some((handle) => !handle)) return null;

  let visible;
  let belongsToContainer;
  let boxes;
  try {
    [visible, belongsToContainer, boxes] = await Promise.all([
      Promise.all(handles.map((handle) => abortable(isVisible(handle), signal))),
      Promise.all(handles.slice(1).map((handle) => abortable(
        challenge.container.evaluate((container, action, element) => (
          action === "contains" && container.contains(element)
        ), "contains", handle),
        signal,
      ))),
      Promise.all(handles.map((handle) => abortable(handle.boundingBox(), signal))),
    ]);
  } catch (error) {
    if (signal?.aborted) throw error;
    return null;
  }
  if (!visible.every(Boolean) || !belongsToContainer.every(Boolean) || !boxes.every(validBox)) {
    return null;
  }

  const [containerBox, targetsBox, backgroundBox, confirmBox] = boxes.map((box) => ({
    x: Number(box.x),
    y: Number(box.y),
    width: Number(box.width),
    height: Number(box.height),
  }));
  if (backgroundBox.width < 120 || backgroundBox.height < 80) return null;
  if (targetsBox.y + targetsBox.height > backgroundBox.y) return null;
  if (overlapArea(targetsBox, backgroundBox) > 0) return null;
  if (overlapArea(confirmBox, targetsBox) > 0 || overlapArea(confirmBox, backgroundBox) > 0) {
    return null;
  }
  return {
    container: containerBox,
    targets: targetsBox,
    background: backgroundBox,
    confirm: confirmBox,
  };
}

async function validateCoordinateClickLayout(challenge, signal) {
  return Boolean(await coordinateClickLayoutSnapshot(challenge, signal));
}

async function geetestContainers(frame) {
  const handles = await safeQueryAll(frame, GEETEST_CONTAINERS);
  if (handles.length) return handles;
  const handle = await safeQuery(frame, GEETEST_CONTAINERS);
  return handle ? [handle] : [];
}

function isTrustedTurnstileUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === "https:" && url.hostname === "challenges.cloudflare.com"
      && (!url.port || url.port === "443") && !url.username && !url.password;
  } catch {
    return false;
  }
}

async function trustedTurnstileIframe(iframe) {
  if (!iframe) return false;
  try {
    const src = await iframe.evaluate((element, operation) => {
      if (operation === "frame-src") return element.src;
      return "";
    }, "frame-src");
    return isTrustedTurnstileUrl(src);
  } catch {
    return false;
  }
}

async function withVerificationCdpSession(page, stage, callback) {
  if (typeof page.createCDPSession !== "function") return null;
  const signal = stage?.signal;
  throwIfAborted(signal);
  const attaching = Promise.resolve().then(() => page.createCDPSession());
  void attaching.then((session) => {
    if (signal?.aborted) return session.detach().catch(() => undefined);
    return undefined;
  }, () => undefined);
  let session;
  try {
    session = await abortable(attaching, signal);
    const send = async (method, params, cleanup = false) => {
      if (!cleanup) throwIfAborted(signal);
      const remaining = stage ? (cleanup ? stage.remainingHard() : stage.remainingAction()) : 1000;
      if (remaining <= 0) throw new VerificationError("timeout");
      const command = session.send(method, params, { timeout: Math.max(1, remaining) });
      return cleanup && stage ? await cleanupOperation(command, stage) : await abortable(command, signal);
    };
    return await callback(send);
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new VerificationError("discovery_failed");
  } finally {
    if (session) {
      let timer;
      try {
        await Promise.race([
          session.detach().catch(() => undefined),
          new Promise((resolve) => { timer = setTimeout(resolve, Math.min(1000, stage?.remainingHard() ?? 1000)); }),
        ]);
      } finally {
        clearTimeout(timer);
      }
    }
  }
}

async function cdpTurnstileFrameState(send, backendNodeId) {
  const { object } = await send("DOM.resolveNode", { backendNodeId });
  if (!object?.objectId) throw new VerificationError("discovery_failed");
  try {
    const result = await send("Runtime.callFunctionOn", {
      objectId: object.objectId,
      returnByValue: true,
      functionDeclaration: `function () {
        if (this.tagName !== 'IFRAME' || !this.isConnected) return null;
        let trusted = false;
        try {
          const url = new URL(this.src);
          trusted = url.protocol === 'https:' && url.hostname === 'challenges.cloudflare.com'
            && (!url.port || url.port === '443') && !url.username && !url.password;
        } catch {}
        const rect = this.getBoundingClientRect();
        const style = getComputedStyle(this);
        const visible = rect.width > 0 && rect.height > 0 && style.display !== 'none'
          && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0
          && (!this.checkVisibility || this.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }));
        return { trusted, visible, box: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          scrollX: window.scrollX, scrollY: window.scrollY };
      }`,
    });
    const state = result.result?.value;
    if (result.exceptionDetails || !state || typeof state.trusted !== "boolean" || typeof state.visible !== "boolean"
      || !state.box || !["x", "y", "width", "height"].every((key) => Number.isFinite(state.box[key]))
      || !Number.isFinite(state.scrollX) || !Number.isFinite(state.scrollY)) {
      throw new VerificationError("discovery_failed");
    }
    return state;
  } finally {
    await send("Runtime.releaseObject", { objectId: object.objectId }, true).catch(() => undefined);
  }
}

async function findClosedShadowTurnstile(page, stage) {
  return await withVerificationCdpSession(page, stage, async (send) => {
    const { root } = await send("DOM.getDocument", { depth: -1, pierce: true });
    if (!root) throw new VerificationError("discovery_failed");
    const pending = [root];
    while (pending.length) {
      throwIfAborted(stage?.signal);
      const node = pending.pop();
      if (node.nodeName === "IFRAME" && node.backendNodeId) {
        const attributes = node.attributes || [];
        const srcIndex = attributes.findIndex((name, index) => index % 2 === 0 && name === "src");
        if (srcIndex >= 0 && isTrustedTurnstileUrl(attributes[srcIndex + 1])) {
          const state = await cdpTurnstileFrameState(send, node.backendNodeId);
          if (state?.trusted && state.visible && validPointerBox(state.box)) {
            return { provider: "turnstile", challenge_type: "checkbox",
              cdpIframe: { page, backendNodeId: node.backendNodeId } };
          }
        }
      }
      pending.push(...(node.children || []), ...(node.shadowRoots || []));
      if (node.contentDocument) pending.push(node.contentDocument);
    }
    return null;
  });
}

async function readClosedShadowTurnstile(challenge, stage) {
  const iframe = challenge.cdpIframe;
  const state = await withVerificationCdpSession(iframe.page, stage,
    (send) => cdpTurnstileFrameState(send, iframe.backendNodeId));
  if (!state?.trusted || !state.visible) throw new VerificationError("checkbox_unavailable");
  if (!validPointerBox(state.box)) throw new VerificationError("geometry_invalid");
  return state;
}

async function findTurnstileCheckbox(frames, iframe = null) {
  const challengeFrames = [];
  try {
    const contentFrame = await iframe?.contentFrame?.();
    if (contentFrame && isTrustedTurnstileUrl(contentFrame.url?.())) challengeFrames.push(contentFrame);
  } catch {}
  for (const frame of frames) {
    let frameUrl = "";
    try {
      frameUrl = String(frame.url?.() || "");
    } catch {
      frameUrl = "";
    }
    if (isTrustedTurnstileUrl(frameUrl) && !challengeFrames.includes(frame)) {
      challengeFrames.push(frame);
    }
  }
  for (const frame of challengeFrames) {
    const checkbox = await firstVisible(frame, TURNSTILE_CHECKBOX_SELECTORS);
    if (checkbox) return checkbox;
  }
  return null;
}

export async function detectVerificationChallenge(page, stage) {
  const mainFrame = page.mainFrame();
  const frames = [];
  for (const frame of [mainFrame, ...(page.frames() || [])]) {
    if (frame && !frames.includes(frame)) frames.push(frame);
  }

  for (const frame of frames) {
    const response = await safeQuery(frame, TURNSTILE_RESPONSE_SELECTOR);
    if (response && await elementValue(response)) {
      return {
        provider: "turnstile",
        challenge_type: "checkbox",
        frame,
        response,
        checkbox: await findTurnstileCheckbox(frames),
      };
    }

    const containers = await geetestContainers(frame);
    for (const container of containers) {
      if (!await isVisible(container)) continue;
      const targets = await firstVisible(container, COORDINATE_TARGET_SELECTORS);
      const coordinateBackground = await firstVisible(container, COORDINATE_BACKGROUND_SELECTORS);
      const confirm = await firstVisible(container, COORDINATE_CONFIRM_SELECTORS);
      if (targets && coordinateBackground && confirm) {
        const challenge = {
          provider: "geetest4",
          challenge_type: "coordinate_click",
          frame,
          container,
          targets,
          background: coordinateBackground,
          confirm,
        };
        if (await validateCoordinateClickLayout(challenge)) return challenge;
      }

      const target = await firstVisible(container, CLICK_TARGET_SELECTORS);
      const candidates = await visibleCandidates(container);
      if (target && candidates.length >= 2) {
        const challenge = {
          provider: "geetest4",
          challenge_type: "icon_click",
          frame,
          container,
          target,
          candidates,
        };
        if (await validateClickLayout(challenge)) return challenge;
      }

      const background = await firstVisible(container, SLIDER_BACKGROUND_SELECTORS);
      const piece = await firstVisible(container, SLIDER_PIECE_SELECTORS);
      const handle = background ? await firstVisible(container, SLIDER_HANDLE_SELECTORS) : null;
      if (handle && background && piece && await isPlausibleSliderHandle(handle, background)) {
        return {
          provider: "geetest4",
          challenge_type: "slider",
          frame,
          container,
          handle,
          background,
          piece,
        };
      }
    }

    const iframe = await safeQuery(frame, TURNSTILE_IFRAME_SELECTOR);
    if (await trustedTurnstileIframe(iframe) && await isVisible(iframe)) {
      return {
        provider: "turnstile",
        challenge_type: "checkbox",
        frame,
        iframe,
        response,
        checkbox: await findTurnstileCheckbox(frames, iframe),
      };
    }
  }
  // OOPIF 位于 closed shadow root 时，Puppeteer 的普通 selector 和 frame 列表可能都不包含它。
  return await findClosedShadowTurnstile(page, stage);
}

async function screenshotBuffer(handle, signal) {
  throwIfAborted(signal);
  const output = await abortable(handle.screenshot({ type: "png" }), signal);
  throwIfAborted(signal);
  const buffer = Buffer.isBuffer(output)
    ? output
    : output instanceof Uint8Array
      ? Buffer.from(output)
      : null;
  if (!buffer?.length) throw new VerificationError("empty_screenshot");
  return buffer;
}

function pngPixelDimensions(buffer) {
  const signature = "89504e470d0a1a0a";
  if (
    !Buffer.isBuffer(buffer)
    || buffer.length < 24
    || buffer.subarray(0, 8).toString("hex") !== signature
    || buffer.readUInt32BE(8) !== 13
    || buffer.subarray(12, 16).toString("ascii") !== "IHDR"
  ) {
    throw new VerificationError("image_format_invalid");
  }
  const width = buffer.readUInt32BE(16);
  const height = buffer.readUInt32BE(20);
  if (width < 1 || width > 32768) throw new VerificationError("image_width_invalid");
  if (height < 1 || height > 32768) throw new VerificationError("image_dimensions_invalid");
  return { width, height };
}

function roundedScreenshotClip(box) {
  const x = Math.round(Number(box.x));
  const y = Math.round(Number(box.y));
  return {
    x,
    y,
    width: Math.round(Number(box.width) + Number(box.x) - x),
    height: Math.round(Number(box.height) + Number(box.y) - y),
  };
}

async function screenshotViewportOffset(handle, signal) {
  const viewportOffset = await abortable(handle.evaluate((_element, action) => {
    if (action !== "viewport-offset" || !window.visualViewport) return null;
    return {
      x: window.visualViewport.pageLeft,
      y: window.visualViewport.pageTop,
    };
  }, "viewport-offset"), signal);
  if (
    !viewportOffset
    || !Number.isFinite(Number(viewportOffset.x))
    || !Number.isFinite(Number(viewportOffset.y))
  ) return null;
  return {
    x: Number(viewportOffset.x),
    y: Number(viewportOffset.y),
  };
}

async function stableElementIdentity(handle, signal) {
  throwIfAborted(signal);
  let identity;
  try {
    identity = await abortable(handle.evaluate((element, action) => {
      if (action !== "fingerprint") return null;
      const media = element.matches?.("img, canvas, svg")
        ? element
        : element.querySelector?.("img, canvas, svg");
      if (!media) {
        const background = window.getComputedStyle(element).backgroundImage;
        const fallback = {
          kind: "element",
          challenge: element.getAttribute?.("data-challenge") || "",
          source: element.getAttribute?.("data-src") || element.getAttribute?.("data-original") || "",
          background: background === "none" ? "" : background,
          text: String(element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200),
        };
        return fallback.challenge || fallback.source || fallback.background || fallback.text
          ? fallback
          : null;
      }
      const tag = String(media.tagName || "").toLowerCase();
      if (tag === "img") {
        return {
          kind: "img",
          source: media.currentSrc || media.getAttribute("src") || "",
          sourceSet: media.getAttribute("srcset") || "",
          width: Number(media.naturalWidth || 0),
          height: Number(media.naturalHeight || 0),
        };
      }
      if (tag === "canvas") {
        return {
          kind: "canvas",
          content: media.toDataURL("image/png"),
          width: Number(media.width || 0),
          height: Number(media.height || 0),
        };
      }
      const clone = media.cloneNode(true);
      for (const node of [clone, ...clone.querySelectorAll("*")]) {
        node.removeAttribute("class");
        node.removeAttribute("style");
        node.removeAttribute("aria-selected");
        node.removeAttribute("aria-checked");
      }
      return { kind: "svg", content: clone.outerHTML };
    }, "fingerprint"), signal);
  } catch (error) {
    if (signal?.aborted) throw error;
    return null;
  }
  throwIfAborted(signal);
  return identity;
}

async function hidePieceAndCaptureBackground(challenge, stage) {
  const { signal } = stage;
  const sessionPromise = stage.page.createCDPSession();
  void sessionPromise.then((lateSession) => {
    if (signal.aborted) return lateSession.detach().catch(() => undefined);
    return undefined;
  }, () => undefined);
  const session = await abortable(sessionPromise, signal);
  let backendNodeId;
  let nodeId;
  let originalStyle;
  let hadStyle = false;
  let hideIssued = false;

  const send = (method, params, cleanup = false) => {
    const remaining = cleanup ? stage.remainingHard() : stage.remainingAction();
    if (!cleanup) {
      throwIfAborted(signal);
      if (remaining <= 0) throw new VerificationError("timeout");
    }
    const command = session.send(method, params, { timeout: Math.max(1, remaining) });
    return cleanup ? cleanupOperation(command, stage) : abortable(command, signal);
  };

  try {
    backendNodeId = await abortable(challenge.piece.backendNodeId(), signal);
    await send("DOM.getDocument", { depth: 0, pierce: true });
    const pushed = await send("DOM.pushNodesByBackendIdsToFrontend", {
      backendNodeIds: [backendNodeId],
    });
    [nodeId] = pushed?.nodeIds || [];
    if (!nodeId) throw new VerificationError("piece_unavailable");
    const attributeResult = await send("DOM.getAttributes", { nodeId });
    const attributes = attributeResult?.attributes || [];
    for (let index = 0; index < attributes.length; index += 2) {
      if (String(attributes[index]).toLowerCase() === "style") {
        hadStyle = true;
        originalStyle = String(attributes[index + 1] || "");
        break;
      }
    }
    const separator = originalStyle && !originalStyle.trimEnd().endsWith(";") ? ";" : "";
    hideIssued = true;
    await send("DOM.setAttributeValue", {
      nodeId,
      name: "style",
      value: `${originalStyle || ""}${separator}visibility:hidden!important;`,
    });
    return await screenshotBuffer(challenge.background, signal);
  } finally {
    try {
      if (hideIssued && nodeId) {
        if (hadStyle) {
          await send("DOM.setAttributeValue", {
            nodeId,
            name: "style",
            value: originalStyle,
          }, true);
        } else {
          await send("DOM.removeAttribute", { nodeId, name: "style" }, true);
        }
      }
    } catch {
      throw new VerificationError("piece_restore_failed");
    } finally {
      const detachPromise = session.detach();
      await cleanupOperation(detachPromise, stage).catch(() => undefined);
    }
  }
}

async function defaultFingerprintChallenge(challenge, stage = {}) {
  const { signal } = stage;
  const hash = createHash("sha256");
  hash.update(`${challenge?.provider || "unknown"}:${challenge?.challenge_type || "unknown"}:`);
  if (challenge?.cdpIframe) {
    const state = await readClosedShadowTurnstile(challenge, stage);
    const image = await abortable(challenge.cdpIframe.page.screenshot({ type: "png", captureBeyondViewport: false,
      clip: { ...state.box, x: state.box.x + state.scrollX, y: state.box.y + state.scrollY } }), signal);
    throwIfAborted(signal);
    hash.update(image);
    return hash.digest("hex");
  }
  if (challenge?.challenge_type === "slider") {
    hash.update(await hidePieceAndCaptureBackground(challenge, stage));
  } else {
    const handles = challenge?.challenge_type === "icon_click"
      ? [challenge.target, ...(challenge.candidates || [])]
      : challenge?.challenge_type === "coordinate_click"
        ? [challenge.targets, challenge.background]
        : [challenge?.checkbox, challenge?.iframe].filter(Boolean);
    for (const handle of handles) {
      const identity = await stableElementIdentity(handle, signal);
      if (identity) hash.update(JSON.stringify(identity));
      else hash.update(await screenshotBuffer(handle, signal));
    }
  }
  if (challenge?.response) {
    hash.update(await abortable(elementValue(challenge.response), signal));
  }
  return hash.digest("hex");
}

async function defaultChallengeComplete(page, challenge, stage) {
  if (challenge?.cdpIframe) return !(await inspectPageVerificationState(page, stage)).detected;
  if (challenge?.provider === "turnstile" && await elementValue(challenge.response)) return true;
  if (challenge?.provider !== "geetest4" || !challenge.frame) return false;
  return Boolean(await firstVisible(challenge.frame, [
    ".geetest_success_radar_tip",
    ".geetest_success",
    "[class*='geetest'][class*='success']",
  ]));
}

async function solveClickChallenge(page, challenge, visionRequest, stage) {
  const { signal } = stage;
  const targetPng = await screenshotBuffer(challenge.target, signal);
  const candidatePngs = await Promise.all(
    challenge.candidates.map((candidate) => screenshotBuffer(candidate, signal)),
  );
  throwIfActionExpired(stage);
  const result = await abortable(visionRequest({
    mode: "click",
    target_png: targetPng.toString("base64"),
    candidate_pngs: candidatePngs.map((buffer) => buffer.toString("base64")),
  }, { signal }), signal);
  throwIfActionExpired(stage);
  const candidateIndex = Number(result?.candidate_index);
  if (!result?.ok || !Number.isInteger(candidateIndex) || !challenge.candidates[candidateIndex]) {
    return { acted: false, reason: String(result?.reason || "vision_rejected"), confidence: result?.confidence };
  }
  if (!await validateClickLayout(challenge, signal)) {
    return { acted: false, reason: "click_layout_invalid", confidence: result.confidence };
  }
  await trustedElementClick(page, challenge.candidates[candidateIndex], stage);
  return { acted: true, confidence: result.confidence };
}

function coordinatePointsAreDistinct(points) {
  for (let leftIndex = 0; leftIndex < points.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < points.length; rightIndex += 1) {
      const deltaX = points[leftIndex].x - points[rightIndex].x;
      const deltaY = points[leftIndex].y - points[rightIndex].y;
      if (Math.hypot(deltaX, deltaY) < 4) return false;
    }
  }
  return true;
}

function isUnitIntervalNumber(value) {
  return typeof value === "number"
    && Number.isFinite(value)
    && value >= 0
    && value <= 1;
}

function validateCoordinatePoints(result, backgroundBox) {
  const points = Array.isArray(result?.points) ? result.points : [];
  if (
    result?.ok !== true
    || points.length < 2
    || points.length > 5
    || !validBox(backgroundBox)
    || !isUnitIntervalNumber(result.confidence)
    || !isUnitIntervalNumber(result.margin)
    || points.some((point) => (
      !isUnitIntervalNumber(point?.x)
      || !isUnitIntervalNumber(point?.y)
      || !isUnitIntervalNumber(point?.confidence)
    ))
  ) {
    return null;
  }
  const converted = points.map((point) => ({
    x: Number(backgroundBox.x) + (point.x * Number(backgroundBox.width)),
    y: Number(backgroundBox.y) + (point.y * Number(backgroundBox.height)),
    confidence: point.confidence,
  }));
  if (converted.some((point) => (
    !Number.isFinite(point.x) || !Number.isFinite(point.y)
    || point.x < Number(backgroundBox.x)
    || point.x > Number(backgroundBox.x) + Number(backgroundBox.width)
    || point.y < Number(backgroundBox.y)
    || point.y > Number(backgroundBox.y) + Number(backgroundBox.height)
  ))) return null;
  return coordinatePointsAreDistinct(converted) ? converted : null;
}

function coordinateLayoutsEqual(left, right) {
  if (!left || !right) return false;
  return ["container", "targets", "background", "confirm"].every((name) => (
    ["x", "y", "width", "height"].every((key) => left[name][key] === right[name][key])
  ));
}

async function currentCoordinateLayoutIfUnchanged(
  challenge,
  initialLayout,
  initialFingerprint,
  fingerprintChallenge,
  stage,
) {
  throwIfActionExpired(stage);
  const currentLayout = await coordinateClickLayoutSnapshot(challenge, stage.signal);
  throwIfActionExpired(stage);
  if (!coordinateLayoutsEqual(initialLayout, currentLayout)) return null;
  const currentFingerprint = await abortable(
    fingerprintChallenge(challenge, stage),
    stage.signal,
  );
  throwIfActionExpired(stage);
  return currentFingerprint === initialFingerprint ? currentLayout : null;
}

async function solveCoordinateClickChallenge(
  page,
  challenge,
  initialFingerprint,
  options,
  visionRequest,
  stage,
) {
  const { signal } = stage;
  if (!challenge?.confirm) return { acted: false, reason: "confirmation_unavailable" };
  const initialLayout = await coordinateClickLayoutSnapshot(challenge, signal);
  if (!initialLayout) return { acted: false, reason: "challenge_changed" };

  const [targetsPng, backgroundPng] = await Promise.all([
    screenshotBuffer(challenge.targets, signal),
    screenshotBuffer(challenge.background, signal),
  ]);
  throwIfActionExpired(stage);
  const result = await abortable(visionRequest({
    mode: "coordinate_click",
    targets_png: targetsPng.toString("base64"),
    background_png: backgroundPng.toString("base64"),
  }, { signal }), signal);
  throwIfActionExpired(stage);
  if (!result?.ok) {
    return {
      acted: false,
      reason: String(result?.reason || "vision_rejected"),
      confidence: result?.confidence,
    };
  }

  const points = validateCoordinatePoints(result, initialLayout.background);
  if (!points) {
    return { acted: false, reason: "coordinate_invalid", confidence: result.confidence };
  }
  const fingerprintChallenge = options.fingerprintChallenge || defaultFingerprintChallenge;
  const currentLayout = await currentCoordinateLayoutIfUnchanged(
    challenge,
    initialLayout,
    initialFingerprint,
    fingerprintChallenge,
    stage,
  );
  if (!currentLayout) {
    return { acted: false, reason: "challenge_changed", confidence: result.confidence };
  }

  const sleep = options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay)));
  const random = options.random || Math.random;
  const mouse = await createMouseDriver(page, stage);
  try {
    for (let index = 0; index < points.length; index += 1) {
      if (index > 0 && !await currentCoordinateLayoutIfUnchanged(
        challenge,
        initialLayout,
        initialFingerprint,
        fingerprintChallenge,
        stage,
      )) {
        return { acted: false, reason: "challenge_changed", confidence: result.confidence };
      }
      const point = points[index];
      throwIfActionExpired(stage);
      await mouse.move(point.x, point.y);
      await mouse.down();
      await mouse.up();
      await abortable(sleep(Math.round(35 + (clampRandom(random) * 40))), signal);
    }
    const confirmationLayout = await currentCoordinateLayoutIfUnchanged(
      challenge,
      initialLayout,
      initialFingerprint,
      fingerprintChallenge,
      stage,
    );
    if (!confirmationLayout) {
      return { acted: false, reason: "challenge_changed", confidence: result.confidence };
    }
    const confirmCenter = {
      x: confirmationLayout.confirm.x + (confirmationLayout.confirm.width / 2),
      y: confirmationLayout.confirm.y + (confirmationLayout.confirm.height / 2),
    };
    await mouse.move(confirmCenter.x, confirmCenter.y);
    await mouse.down();
    await mouse.up();
  } finally {
    await mouse.up(signal.aborted).catch(() => undefined);
    await mouse.close();
  }
  return { acted: true, confidence: result.confidence };
}

async function captureSliderImages(challenge, stage) {
  const { signal } = stage;
  const backgroundBuffer = await hidePieceAndCaptureBackground(challenge, stage);
  const pieceBuffer = await screenshotBuffer(challenge.piece, signal);
  return {
    backgroundBuffer,
    pieceBuffer,
  };
}

async function createMouseDriver(page, stage) {
  const { signal } = stage;
  const sessionPromise = page.createCDPSession();
  void sessionPromise.then((lateSession) => {
    if (signal.aborted) return lateSession.detach().catch(() => undefined);
    return undefined;
  }, () => undefined);
  const session = await abortable(sessionPromise, signal);
  let x = 0;
  let y = 0;
  let mouseDown = false;
  const inFlight = new Set();

  const send = async (params, cleanup = false) => {
    const remaining = cleanup ? stage.remainingHard() : stage.remainingAction();
    if (!cleanup) {
      throwIfAborted(signal);
      if (remaining <= 0) throw new VerificationError("timeout");
    }
    const command = session.send(
      "Input.dispatchMouseEvent",
      { pointerType: "mouse", ...params },
      { timeout: Math.max(1, remaining) },
    );
    inFlight.add(command);
    void command.finally(() => inFlight.delete(command)).catch(() => undefined);
    return cleanup
      ? await cleanupOperation(command, stage)
      : await abortable(command, signal);
  };

  const mouse = {
    async move(nextX, nextY) {
      throwIfAborted(signal);
      x = nextX;
      y = nextY;
      await send({
        type: "mouseMoved",
        x,
        y,
        buttons: mouseDown ? 1 : 0,
        button: mouseDown ? "left" : "none",
      });
    },
    async down() {
      throwIfAborted(signal);
      mouseDown = true;
      await send({ type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
    },
    async up(cleanup = false) {
      if (!mouseDown) return;
      if (cleanup) {
        try {
          await send({ type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 }, true);
        } finally {
          mouseDown = false;
        }
        return;
      }
      await send({ type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 }, cleanup);
      mouseDown = false;
    },
    async close() {
      if (mouseDown) await mouse.up(true).catch(() => undefined);
      if (inFlight.size > 0) {
        await cleanupOperation(Promise.allSettled([...inFlight]), stage).catch(() => undefined);
      }
      await cleanupOperation(session.detach(), stage).catch(() => undefined);
    },
  };
  return mouse;
}

async function trustedElementClick(page, handle, stage) {
  const { signal } = stage;
  const box = await abortable(handle.boundingBox(), signal);
  if (!box || box.width <= 0 || box.height <= 0) {
    throw new VerificationError("click_target_unavailable");
  }
  const mouse = await createMouseDriver(page, stage);
  try {
    await mouse.move(box.x + (box.width / 2), box.y + (box.height / 2));
    await mouse.down();
    await mouse.up();
  } finally {
    await mouse.close();
  }
}

function validPointerBox(box) {
  return Boolean(box) && ["x", "y", "width", "height"].every((key) => Number.isFinite(box[key]))
    && box.x >= 0 && box.y >= 0 && box.width > 0 && box.height > 0;
}

async function trustedTurnstileClick(page, challenge, stage) {
  const { signal } = stage;
  let frameBox = null;
  if (challenge.cdpIframe) {
    frameBox = (await readClosedShadowTurnstile(challenge, stage)).box;
  } else if (challenge.iframe) {
    if (!await abortable(trustedTurnstileIframe(challenge.iframe), signal)
      || !await abortable(isVisible(challenge.iframe), signal)) {
      throw new VerificationError("checkbox_unavailable");
    }
    frameBox = await abortable(challenge.iframe.boundingBox(), signal);
    if (!validPointerBox(frameBox)) throw new VerificationError("geometry_invalid");
  }
  let x;
  let y;
  if (challenge.checkbox && await abortable(isVisible(challenge.checkbox), signal)) {
    const box = await abortable(challenge.checkbox.boundingBox(), signal);
    if (!validPointerBox(box)) throw new VerificationError("geometry_invalid");
    x = box.x + (box.width / 2);
    y = box.y + (box.height / 2);
  } else {
    // 官方可见小部件的复选框可能位于 closed shadow DOM，限定在小部件内交互。
    if (!frameBox || frameBox.width < 200 || frameBox.height < 50
      || frameBox.width > 600 || frameBox.height > 150) {
      throw new VerificationError("checkbox_unavailable");
    }
    x = frameBox.x + 30;
    y = frameBox.y + (frameBox.height / 2);
  }
  if (frameBox && !(x >= frameBox.x && x <= frameBox.x + frameBox.width
    && y >= frameBox.y && y <= frameBox.y + frameBox.height)) {
    throw new VerificationError("geometry_invalid");
  }
  throwIfActionExpired(stage);
  const mouse = await createMouseDriver(page, stage);
  try {
    const startX = Math.max(0, x - 40);
    const startY = Math.max(0, y - 25);
    for (let index = 0; index <= 12; index++) {
      const progress = index / 12;
      const eased = progress * progress * (3 - (2 * progress));
      await mouse.move(startX + ((x - startX) * eased), startY + ((y - startY) * eased));
    }
    if (challenge.cdpIframe) {
      const currentBox = (await readClosedShadowTurnstile(challenge, stage)).box;
      if (["x", "y", "width", "height"].some((key) => Math.abs(currentBox[key] - frameBox[key]) > 0.5)) {
        throw new VerificationError("geometry_invalid");
      }
    }
    await mouse.down();
    await mouse.up();
  } finally {
    await mouse.close();
  }
}

async function solveSliderChallenge(page, challenge, options, visionRequest, stage) {
  const { signal } = stage;
  const settled = await Promise.allSettled([
    abortable(challenge.handle.boundingBox(), signal),
    abortable(challenge.background.boundingBox(), signal),
    abortable(challenge.piece.boundingBox(), signal),
    captureSliderImages(challenge, stage),
    screenshotViewportOffset(challenge.background, signal),
  ]);
  const failed = settled.find((result) => result.status === "rejected");
  if (failed) throw failed.reason;
  const [handleBox, backgroundBox, pieceBox, images, viewportOffset] = (
    settled.map((result) => result.value)
  );
  if (![handleBox, backgroundBox, pieceBox].every(validBox) || !viewportOffset) {
    return { acted: false, reason: "slider_geometry_invalid" };
  }
  throwIfActionExpired(stage);
  const backgroundPixels = pngPixelDimensions(images.backgroundBuffer);
  const backgroundClip = roundedScreenshotClip({
    ...backgroundBox,
    x: Number(backgroundBox.x) + viewportOffset.x,
    y: Number(backgroundBox.y) + viewportOffset.y,
  });
  const pieceClip = roundedScreenshotClip({
    ...pieceBox,
    x: Number(pieceBox.x) + viewportOffset.x,
    y: Number(pieceBox.y) + viewportOffset.y,
  });
  if (!validBox(backgroundClip) || !validBox(pieceClip)) {
    return { acted: false, reason: "slider_geometry_invalid" };
  }
  const pixelScaleX = backgroundPixels.width / backgroundClip.width;
  const pixelScaleY = backgroundPixels.height / backgroundClip.height;
  const result = await abortable(visionRequest({
    mode: "slider",
    background_png: images.backgroundBuffer.toString("base64"),
    piece_png: images.pieceBuffer.toString("base64"),
    geometry: {
      image_width: backgroundPixels.width,
      image_height: backgroundPixels.height,
      track_width: backgroundBox.width,
      track_height: backgroundBox.height,
      handle_width: handleBox.width,
      piece_offset_x: (pieceClip.x - backgroundClip.x) * pixelScaleX,
      piece_offset_y: (pieceClip.y - backgroundClip.y) * pixelScaleY,
    },
  }, { signal }), signal);
  throwIfActionExpired(stage);
  const detectedDistance = Number(result?.distance_css);
  if (!result?.ok || !Number.isFinite(detectedDistance)) {
    return { acted: false, reason: String(result?.reason || "vision_rejected"), confidence: result?.confidence };
  }

  const random = options.random || Math.random;
  const sleep = options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay)));
  const handleOffsetX = handleBox.x - backgroundBox.x;
  const distance = detectedDistance - handleOffsetX;
  if (!Number.isFinite(distance) || distance < 0) {
    return { acted: false, reason: "distance_invalid", confidence: result.confidence };
  }
  const startX = handleBox.x + (handleBox.width / 2);
  const startY = handleBox.y + (handleBox.height / 2);
  const trajectory = buildDragTrajectory(distance, random);
  const minimumCenterX = backgroundBox.x + (handleBox.width / 2);
  const maximumCenterX = backgroundBox.x + backgroundBox.width - (handleBox.width / 2);
  const trajectoryValid = trajectory.every((point) => (
    Number.isFinite(point.x)
    && Number.isFinite(point.y)
    && startX + point.x >= minimumCenterX - 0.5
    && startX + point.x <= maximumCenterX + 0.5
  ));
  if (!trajectoryValid) {
    return { acted: false, reason: "trajectory_out_of_bounds", confidence: result.confidence };
  }
  const mouse = await createMouseDriver(page, stage);
  try {
    await mouse.move(startX, startY);
    await mouse.down();
    for (const point of trajectory) {
      throwIfAborted(signal);
      await mouse.move(startX + point.x, startY + point.y);
      await abortable(sleep(Math.round(10 + (clampRandom(random) * 25))), signal);
    }
  } finally {
    await mouse.up(signal.aborted).catch(() => undefined);
    await mouse.close();
  }
  return { acted: true, confidence: result.confidence };
}

async function turnstileResponseReady(page, challenge, options, stage) {
  const { signal } = stage;
  const complete = options.isChallengeComplete || defaultChallengeComplete;
  const waitMs = Math.max(0, Math.min(
    Number(options.turnstileResponseWaitMs ?? DEFAULT_TURNSTILE_WAIT_MS) || 0,
    AUTO_VERIFY_TIMEOUT_MS,
  ));
  const waitDeadline = performance.now() + waitMs;
  do {
    if (await abortable(complete(page, challenge, stage), signal)) return true;
    if (performance.now() >= waitDeadline) break;
    await abortable(
      (options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay))))(
        Math.min(POLL_INTERVAL_MS, waitDeadline - performance.now()),
      ),
      signal,
    );
  } while (performance.now() <= waitDeadline);
  return false;
}

async function waitForInitialChallenge(page, options, stage) {
  const { signal } = stage;
  const detect = options.detectChallenge || detectVerificationChallenge;
  const sleep = options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay)));
  do {
    throwIfActionExpired(stage);
    // 页面级 CF 可能在挂载可交互小部件前自行放行，不能只等待 widget。
    if (options.isInitialChallengeComplete
      && await abortable(options.isInitialChallengeComplete(page), signal)) {
      throwIfActionExpired(stage);
      return null;
    }
    const challenge = await abortable(detect(page, stage), signal);
    throwIfActionExpired(stage);
    if (challenge) return challenge;
    const remaining = stage.remainingAction();
    if (remaining <= 0) break;
    await abortable(sleep(Math.min(POLL_INTERVAL_MS, remaining)), signal);
    throwIfActionExpired(stage);
  } while (performance.now() <= stage.actionDeadline);
  throwIfActionExpired(stage);
  return null;
}

async function waitForOutcome(page, challenge, fingerprint, options, stage) {
  const { signal } = stage;
  const detect = options.detectChallenge || detectVerificationChallenge;
  const complete = options.isChallengeComplete || defaultChallengeComplete;
  const fingerprintChallenge = options.fingerprintChallenge || defaultFingerprintChallenge;
  const sleep = options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay)));
  let latestChallenge = challenge;
  let observedUnchanged = false;

  try {
    do {
      throwIfActionExpired(stage);
      const latestComplete = await abortable(complete(page, latestChallenge, stage), signal);
      throwIfActionExpired(stage);
      if (latestComplete) {
        return { completed: true, changed: false, challenge: latestChallenge };
      }
      const current = await abortable(detect(page, stage), signal);
      throwIfActionExpired(stage);
      if (current) {
        latestChallenge = current;
        const currentComplete = await abortable(complete(page, current, stage), signal);
        throwIfActionExpired(stage);
        if (currentComplete) {
          return { completed: true, changed: false, challenge: current };
        }
        const currentFingerprint = await abortable(fingerprintChallenge(current, stage), signal);
        throwIfActionExpired(stage);
        if (currentFingerprint !== fingerprint) {
          return { completed: false, changed: true, challenge: current };
        }
        observedUnchanged = true;
      }
      const remaining = stage.remainingAction();
      if (remaining <= 0) break;
      await abortable(sleep(Math.min(POLL_INTERVAL_MS, remaining)), signal);
      throwIfActionExpired(stage);
    } while (performance.now() <= stage.actionDeadline);
    throwIfActionExpired(stage);
  } catch (error) {
    const actionTimedOut = signal.reason?.reason === "timeout"
      || (!signal.aborted && stage.remainingAction() <= 0 && error?.reason === "timeout");
    if (observedUnchanged && actionTimedOut) {
      return { completed: false, changed: false, challenge: latestChallenge };
    }
    throw error;
  }
  return { completed: false, changed: false, challenge: latestChallenge };
}

function failureReason(error, signal, fallback) {
  if (signal.aborted) return String(signal.reason?.reason || "aborted");
  return String(error?.reason || fallback);
}

async function attemptWithinStage(page, options, stage) {
  const { signal } = stage;
  const fingerprintChallenge = options.fingerprintChallenge || defaultFingerprintChallenge;
  const visionRequest = options.visionRequest
    || ((payload, requestOptions) => runVisionRequest(payload, requestOptions));
  let challenge;
  try {
    challenge = await waitForInitialChallenge(page, options, stage);
  } catch (error) {
    return sanitizeVerificationResult({
      reason: failureReason(error, signal, "discovery_failed"),
    });
  }
  if (!challenge) {
    return sanitizeVerificationResult({ completed: true, reason: "no_challenge" });
  }

  const summary = {
    attempted: false,
    completed: false,
    provider: challenge.provider,
    challenge_type: challenge.challenge_type,
    attempts: 0,
    reason: "",
  };
  const maxAttempts = boundedAttempts(options.maxAttempts);
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      throwIfActionExpired(stage);
    } catch (error) {
      summary.reason = failureReason(error, signal, "timeout");
      break;
    }
    let fingerprint;
    let interaction;
    try {
      if (challenge.provider === "turnstile") {
        const responseReady = await turnstileResponseReady(page, challenge, options, stage);
        throwIfActionExpired(stage);
        if (responseReady) {
          summary.completed = true;
          summary.reason = "completed";
          break;
        }
        fingerprint = await abortable(fingerprintChallenge(challenge, stage), signal);
        throwIfActionExpired(stage);
        if (!challenge.checkbox && !challenge.iframe && !challenge.cdpIframe) {
          summary.reason = "checkbox_unavailable";
          break;
        }
        summary.attempted = true;
        summary.attempts = attempt;
        await trustedTurnstileClick(page, challenge, stage);
        interaction = { acted: true };
      } else if (challenge.challenge_type === "coordinate_click") {
        fingerprint = await abortable(fingerprintChallenge(challenge, stage), signal);
        throwIfActionExpired(stage);
        summary.attempted = true;
        summary.attempts = attempt;
        interaction = await solveCoordinateClickChallenge(
          page,
          challenge,
          fingerprint,
          options,
          visionRequest,
          stage,
        );
      } else if (challenge.challenge_type === "icon_click") {
        fingerprint = await abortable(fingerprintChallenge(challenge, stage), signal);
        throwIfActionExpired(stage);
        summary.attempted = true;
        summary.attempts = attempt;
        interaction = await solveClickChallenge(page, challenge, visionRequest, stage);
      } else if (challenge.challenge_type === "slider") {
        fingerprint = await abortable(fingerprintChallenge(challenge, stage), signal);
        throwIfActionExpired(stage);
        summary.attempted = true;
        summary.attempts = attempt;
        interaction = await solveSliderChallenge(page, challenge, options, visionRequest, stage);
      } else {
        interaction = { acted: false, reason: "challenge_unsupported" };
      }
    } catch (error) {
      summary.reason = failureReason(error, signal, "interaction_failed");
      break;
    }
    try {
      throwIfActionExpired(stage);
    } catch (error) {
      summary.reason = failureReason(error, signal, "timeout");
      break;
    }
    if (Number.isFinite(Number(interaction?.confidence))) {
      summary.confidence = Number(interaction.confidence);
    }
    if (!interaction?.acted) {
      summary.reason = interaction?.reason || "interaction_failed";
      break;
    }

    let outcome;
    try {
      outcome = await waitForOutcome(page, challenge, fingerprint, options, stage);
    } catch (error) {
      summary.reason = failureReason(error, signal, "outcome_failed");
      break;
    }
    if (outcome.completed) {
      summary.completed = true;
      summary.reason = "completed";
      break;
    }
    if (!outcome.changed || !outcome.challenge) {
      summary.reason = "challenge_unchanged";
      break;
    }
    if (attempt === maxAttempts) {
      summary.reason = "attempts_exhausted";
      break;
    }
    challenge = outcome.challenge;
    summary.provider = challenge.provider;
    summary.challenge_type = challenge.challenge_type;
  }
  return sanitizeVerificationResult(summary);
}

export async function attemptAutomaticVerification(page, options = {}) {
  const requestedTimeout = Number(options.timeoutMs ?? AUTO_VERIFY_TIMEOUT_MS);
  const timeoutMs = Math.max(1, Math.min(
    Number.isFinite(requestedTimeout) ? requestedTimeout : AUTO_VERIFY_TIMEOUT_MS,
    AUTO_VERIFY_TIMEOUT_MS,
  ));
  const stage = createStageSignal(timeoutMs, options.signal);
  stage.page = page;
  try {
    return await attemptWithinStage(page, options, stage);
  } finally {
    stage.cleanup();
  }
}

function boundedAttempts(value) {
  const requested = Number(value ?? MAX_PROVIDER_ATTEMPTS);
  return Number.isFinite(requested)
    ? Math.max(1, Math.min(MAX_PROVIDER_ATTEMPTS, Math.floor(requested)))
    : MAX_PROVIDER_ATTEMPTS;
}

// 只返回验证状态，不读取或传出验证码令牌、Cookie 或页面正文。
export async function inspectPageVerificationState(page, stage) {
  const state = await page.evaluate(() => {
    const visible = (element) => {
      if (!element) return false;
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return Number.isFinite(box.width) && Number.isFinite(box.height)
        && box.width > 0 && box.height > 0 && style.display !== "none"
        && style.visibility !== "hidden" && Number(style.opacity) !== 0;
    };
    const title = String(document.title || "").trim();
    const challengeTitle = /^(?:just a moment|attention required|请稍候|请稍等|請稍候)/i.test(title);
    const challengeMarkup = document.querySelector(
      "#challenge-form, #challenge-running, #cf-challenge-running, script[src*='/cdn-cgi/challenge-platform/']",
    );
    if (challengeTitle && challengeMarkup) return { detected: true, kind: "cloudflare" };
    for (const iframe of document.querySelectorAll("iframe[src]")) {
      if (!visible(iframe)) continue;
      try {
        const url = new URL(iframe.src, location.href);
        if (url.protocol === "https:" && url.hostname === "challenges.cloudflare.com"
          && (!url.port || url.port === "443") && !url.username && !url.password) {
          return { detected: true, kind: "cloudflare" };
        }
      } catch {}
    }
    for (const container of document.querySelectorAll(".geetest_box, .geetest_panel")) {
      if (!visible(container)) continue;
      const success = container.querySelector(
        ".geetest_success_radar_tip, .geetest_success, [class*='geetest'][class*='success']",
      );
      if (!visible(success)) return { detected: true, kind: "geetest4" };
    }
    return { detected: false, kind: "none" };
  });
  if (state.detected) return state;
  return await findClosedShadowTurnstile(page, stage)
    ? { detected: true, kind: "cloudflare" } : state;
}

// 先等待页面自然完成，再在同一截止时间内复用已有的有限验证流程。
export async function ensurePageVerification(page, options = {}) {
  const requestedTimeout = Number(options.timeoutMs ?? AUTO_VERIFY_TIMEOUT_MS);
  const timeoutMs = Math.max(1, Math.min(
    Number.isFinite(requestedTimeout) ? requestedTimeout : AUTO_VERIFY_TIMEOUT_MS,
    AUTO_VERIFY_TIMEOUT_MS,
  ));
  const stage = createStageSignal(timeoutMs, options.signal);
  stage.page = page;
  const inspect = options.detectPageState || inspectPageVerificationState;
  const sleep = options.sleep || ((delay) => new Promise((resolve) => setTimeout(resolve, delay)));
  let detected = false;
  let kind = "none";
  let verification = sanitizeVerificationResult({ completed: true, reason: "no_challenge" });
  const result = (completed, reason) => ({
    detected,
    completed,
    manual_required: !completed && detected,
    kind,
    reason,
    verification,
  });
  const inspectCurrent = async () => {
    while (true) {
      throwIfActionExpired(stage);
      try {
        const state = await abortable(inspect(page, stage), stage.signal);
        throwIfActionExpired(stage);
        if (state?.detected && ["cloudflare", "geetest4"].includes(state.kind)) {
          detected = true;
          if (kind === "none") kind = state.kind;
          return true;
        }
        return false;
      } catch (error) {
        if (!/execution context (?:was )?destroyed|cannot find context with specified id|execution context is not available in detached frame/i.test(String(error?.message || ""))) {
          throw error;
        }
        throwIfActionExpired(stage);
        await abortable(sleep(Math.min(POLL_INTERVAL_MS, stage.remainingAction())), stage.signal);
      }
    }
  };
  try {
    if (!await inspectCurrent()) return result(true, "no_challenge");
    verification = sanitizeVerificationResult({ reason: "no_challenge" });
    const requestedPassiveWait = Number(options.passiveWaitMs ?? 5_000);
    const passiveWaitMs = Math.max(0, Math.min(
      Number.isFinite(requestedPassiveWait) ? requestedPassiveWait : 5_000,
      stage.remainingAction(),
    ));
    const passiveDeadline = performance.now() + passiveWaitMs;
    while (performance.now() < passiveDeadline) {
      await abortable(sleep(Math.min(POLL_INTERVAL_MS, passiveDeadline - performance.now())), stage.signal);
      if (!await inspectCurrent()) {
        verification = sanitizeVerificationResult({ completed: true, reason: "completed" });
        return result(true, "completed");
      }
    }
    if (options.allowInteraction === false) {
      verification = sanitizeVerificationResult({ reason: "interaction_disabled" });
      return result(false, "interaction_disabled");
    }
    const solverOptions = {
      ...options,
      maxAttempts: boundedAttempts(options.maxAttempts),
      signal: stage.signal,
      timeoutMs: stage.remainingAction(),
      isInitialChallengeComplete: async () => !await inspectCurrent(),
      // 有 token 不等于整页放行；只在验证界面消失后允许后续请求。
      isChallengeComplete: async () => !await inspectCurrent(),
    };
    verification = sanitizeVerificationResult(options.verificationAdapter
      ? await abortable(options.verificationAdapter(page, solverOptions), stage.signal)
      : await attemptWithinStage(page, solverOptions, stage));
    if (!await inspectCurrent()) return result(true, "completed");
    return result(false, verification.completed ? "challenge_unchanged" : verification.reason);
  } catch (error) {
    const reason = failureReason(error, stage.signal, "discovery_failed");
    verification = sanitizeVerificationResult({ ...verification, completed: false, reason });
    return result(false, verification.reason);
  } finally {
    stage.cleanup();
  }
}
