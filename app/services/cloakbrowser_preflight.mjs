import { hasMakerWorldSessionCookie, normalizePlatform } from "./cloakbrowser_login.mjs";
import { ensurePageVerification, sanitizeVerificationResult } from "./cloakbrowser_verification.mjs";

function modelTarget(value, platform) {
  try {
    const url = new URL(String(value || ""));
    const host = normalizePlatform(platform) === "global" ? "makerworld.com" : "makerworld.com.cn";
    const match = url.pathname.match(/^\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?models\/([1-9][0-9]*)(?:[-/]|$)/i);
    if (url.protocol !== "https:" || url.hostname !== host || url.username || url.password
      || (url.port && url.port !== "443") || !match) return null;
    return { model_id: match[1], model_url: `${url.origin}${url.pathname}` };
  } catch {
    return null;
  }
}

function targetError(code) {
  const error = new Error(code === "MAKERHUB_TARGET_MISMATCH"
    ? "model page selected a different 3MF instance" : "requested 3MF instance is unavailable");
  error.code = code;
  return error;
}

export async function readThreeMfModelState(page, platform, modelUrl) {
  const expected = modelTarget(modelUrl, platform);
  const current = modelTarget(page.url(), platform);
  if (!expected || !current || current.model_id !== expected.model_id) {
    return { reason: "model_mismatch", model: null, login_visible: false, hash: "" };
  }
  const state = await page.evaluate(({ modelId, modelUrl: cleanModelUrl }) => {
    const numericId = (value) => {
      if (typeof value === "number" && !Number.isSafeInteger(value)) return "";
      if (!["number", "string"].includes(typeof value)) return "";
      const text = String(value);
      return /^[1-9][0-9]*$/.test(text) ? text : "";
    };
    const restrictions = (value) => {
      const result = {};
      if (typeof value.threeMfDownloadAllowed === "boolean") result.threeMfDownloadAllowed = value.threeMfDownloadAllowed;
      if (value.threeMfSkipReason === "crowdfunding") result.threeMfSkipReason = "crowdfunding";
      if (value.downloadState === "not_downloadable") result.downloadState = "not_downloadable";
      if (value.paidSetting && typeof value.paidSetting === "object") {
        result.paidSetting = { crowdfunding: [true, 1, "1", "true"].includes(value.paidSetting.crowdfunding) };
      }
      const projectId = numericId(value.crowdfundingInfo?.projectId);
      if (projectId) result.crowdfundingInfo = { projectId };
      // 只传出许可限制语义，不传出作者正文或任意嵌套字段。
      for (const key of ["license", "licenseName", "licenseDescriptionInfo"]) {
        const raw = value[key];
        const text = typeof raw === "string" ? raw : raw && typeof raw === "object"
          ? [raw.title, raw.content, raw.description].filter((part) => typeof part === "string").join(" ") : "";
        if (/sdfl-ppo|platform print only/i.test(text)) result[key] = "SDFL-PPO";
      }
      return result;
    };
    const visible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0"
        && box.width > 0 && box.height > 0;
    };
    const loginVisible = [...document.querySelectorAll("button, a, [role='button']")].some((element) => (
      visible(element) && /^(?:登录|登入|登錄|log\s*in|sign\s*in)$/i.test(
        String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim(),
      )
    ));
    const base = { login_visible: loginVisible, hash: String(location.hash || "").slice(0, 100) };
    let design;
    try {
      design = JSON.parse(document.querySelector("#__NEXT_DATA__")?.textContent || "{}")?.props?.pageProps?.design;
    } catch {
      return { ...base, reason: "model_unavailable", model: null };
    }
    if (!design || typeof design !== "object") return { ...base, reason: "model_unavailable", model: null };
    if (numericId(design.id) !== modelId) return { ...base, reason: "model_mismatch", model: null };
    const instances = (Array.isArray(design.instances) ? design.instances : []).slice(0, 100)
      .filter((instance) => instance && typeof instance === "object" && numericId(instance.id || instance.instanceId))
      .map((instance) => ({ id: numericId(instance.id || instance.instanceId), ...restrictions(instance) }));
    return { ...base, reason: instances.length ? "ready" : "instances_unavailable",
      model: { model_id: modelId, model_url: cleanModelUrl, instances, ...restrictions(design) } };
  }, { modelId: expected.model_id, modelUrl: current.model_url });
  return state;
}

export async function assertThreeMfTarget(page, { platform, modelUrl, instanceId, timeoutMs = 5000 }) {
  const expectedId = String(instanceId || "");
  const timeout = Math.max(0, Math.min(Number(timeoutMs) || 0, 5000));
  const deadline = Date.now() + timeout;
  while (true) {
    const state = await readThreeMfModelState(page, platform, modelUrl);
    if (!/^[1-9][0-9]*$/.test(expectedId)
      || !state.model?.instances.some((instance) => instance.id === expectedId)) {
      throw targetError("MAKERHUB_TARGET_UNAVAILABLE");
    }
    if (state.hash === `#profileId-${expectedId}`) return;
    if (Date.now() >= deadline) throw targetError("MAKERHUB_TARGET_MISMATCH");
    await new Promise((resolve) => setTimeout(resolve, Math.min(100, deadline - Date.now())));
  }
}

export async function installThreeMfAuthorizationGuard(page, { platform, instanceId = "", readOnly = false }) {
  const controller = new AbortController();
  const tasks = new Set();
  const hosts = normalizePlatform(platform) === "global"
    ? ["makerworld.com", "api.bambulab.com"] : ["makerworld.com.cn", "api.bambulab.cn"];
  const onRequest = (request) => {
    const task = (async () => {
      const url = new URL(request.url());
      const authorization = url.pathname.match(/^\/(?:api\/)?v1\/design-service\/instance\/([0-9]+)\/f3mf\/?$/i);
      const blocked = authorization && (readOnly || authorization[1] !== String(instanceId)
        || url.protocol !== "https:" || !hosts.includes(url.hostname)
        || url.username || url.password || (url.port && url.port !== "443"));
      if (blocked || (readOnly && /\.3mf\/?$/i.test(url.pathname))) {
        await request.abort("blockedbyclient");
        if (!readOnly) controller.abort(targetError("MAKERHUB_TARGET_MISMATCH"));
      } else {
        await request.continue();
      }
    })();
    tasks.add(task);
    void task.catch(() => controller.abort(targetError("MAKERHUB_TARGET_UNAVAILABLE")))
      .finally(() => tasks.delete(task));
  };
  page.on("request", onRequest);
  try {
    await page.setRequestInterception(true);
  } catch (error) {
    page.off("request", onRequest);
    throw error;
  }
  return { signal: controller.signal, dispose: async () => {
    page.off("request", onRequest);
    await Promise.allSettled([...tasks]);
  } };
}

export async function prepareThreeMfDownload(input, dependencies) {
  const { browser, context, platform, modelUrl } = input;
  const target = modelTarget(modelUrl, platform);
  const failed = (reason, extra = {}) => ({ ready: false, reason, authenticated: false, model: null, ...extra });
  if (!target) return failed("model_unavailable");
  let releaseGuard = async () => {};
  try {
    return await dependencies.withTemporaryPage(browser, context, async (page) => {
      const guard = await installThreeMfAuthorizationGuard(page, { platform, readOnly: true });
      const verificationController = new AbortController();
      const cancelVerification = () => verificationController.abort();
      guard.signal.addEventListener("abort", cancelVerification, { once: true });
      releaseGuard = async () => {
        verificationController.abort();
        guard.signal.removeEventListener("abort", cancelVerification);
        await guard.dispose();
      };
      try {
        await page.goto(target.model_url, { waitUntil: "domcontentloaded",
          timeout: Math.max(1000, Math.min(Number(input.navigationTimeoutMs) || 30000, 120000)) });
      } catch (error) {
        if (error?.name !== "TimeoutError") return failed("preflight_failed");
      }
      if (!modelTarget(page.url(), platform)) return failed("model_mismatch");
      const verify = dependencies.ensurePageVerification || ensurePageVerification;
      const allowInteraction = input.autoVerifyChallenges !== false;
      const verificationTimeoutMs = allowInteraction
        ? Math.max(1, Math.min(Number(input.verificationTimeoutMs) || 50000, 50000)) : 5000;
      const verification = await verify(page, {
        timeoutMs: verificationTimeoutMs,
        passiveWaitMs: Math.min(5000, verificationTimeoutMs),
        allowInteraction,
        maxAttempts: allowInteraction ? Math.max(1, Math.min(Math.floor(Number(input.verificationMaxAttempts) || 2), 2)) : 1,
        signal: verificationController.signal,
      });
      if (guard.signal.aborted) return failed("preflight_failed");
      if (!verification.completed) return failed("verification_required", {
        verification: sanitizeVerificationResult(verification.verification),
      });
      const state = await readThreeMfModelState(page, platform, target.model_url);
      if (state.reason !== "ready") return failed(state.reason, { model: state.model });
      const cookies = (await context.cookies()).filter((cookie) => (
        !Number.isFinite(cookie.expires) || cookie.expires <= 0 || cookie.expires > Date.now() / 1000
      ));
      const authenticated = hasMakerWorldSessionCookie(cookies, platform, new URL(page.url()).pathname) && !state.login_visible;
      return { ready: authenticated, reason: authenticated ? "ready" : "auth_required", authenticated, model: state.model };
    });
  } catch {
    return failed("preflight_failed");
  } finally {
    await releaseGuard();
  }
}
