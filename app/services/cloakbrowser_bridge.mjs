import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  browserAuthTokenForUrl,
  completeBambuLoginConfirmation,
  hasMakerWorldSessionCookie,
  isBambuLoginConfirmationUrl,
  isMakerWorldUrl,
  makerWorldHomeUrl,
  normalizePlatform,
} from "./cloakbrowser_login.mjs";
import {
  attemptAutomaticVerification,
  AUTO_VERIFY_TIMEOUT_MS,
  sanitizeVerificationResult,
} from "./cloakbrowser_verification.mjs";


const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(__dirname, "..", "..");
const requireFromFrontend = createRequire(path.join(ROOT_DIR, "frontend", "node_modules", "package.json"));
const puppeteer = requireFromFrontend("puppeteer-core");
const BROWSER_FETCH_TOTAL_BUFFER_BYTES = 32 * 1024 * 1024;
const BROWSER_FETCH_RESOURCE_BUFFER_BYTES = 24 * 1024 * 1024;

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8").trim();
  return text ? JSON.parse(text) : {};
}

function authHeaders(token) {
  if (!token) throw new Error("auth_token is required");
  return { Authorization: `Bearer ${token}` };
}

async function resolveWebSocketEndpoint(cdpUrl, headers) {
  const response = await fetch(`${String(cdpUrl || "").replace(/\/$/, "")}/json/version`, { headers });
  if (!response.ok) {
    throw new Error(`CDP endpoint returned HTTP ${response.status}`);
  }
  const payload = await response.json();
  if (!payload?.webSocketDebuggerUrl) {
    throw new Error("CDP endpoint did not return webSocketDebuggerUrl");
  }
  return payload.webSocketDebuggerUrl;
}

function cleanCookie(item) {
  if (!item || typeof item !== "object") return null;
  const name = String(item.name || "").trim();
  const value = String(item.value ?? "");
  if (!name || value === "") return null;
  const cookie = {
    name,
    value,
    path: String(item.path || "/") || "/",
    secure: item.secure !== false,
  };
  if (item.domain) cookie.domain = String(item.domain);
  else if (item.url) cookie.url = String(item.url);
  else return null;
  if (typeof item.httpOnly === "boolean") cookie.httpOnly = item.httpOnly;
  if (typeof item.expires === "number" && Number.isFinite(item.expires)) cookie.expires = item.expires;
  if (["Strict", "Lax", "None"].includes(item.sameSite)) cookie.sameSite = item.sameSite;
  return cookie;
}

async function storageSnapshot(page) {
  const url = page.url();
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    return await page.evaluate(() => {
      const select = (storage) => {
        const result = {};
        for (let index = 0; index < storage.length; index += 1) {
          const key = storage.key(index);
          if (!key || !/token|auth|session/i.test(key)) continue;
          const value = storage.getItem(key);
          if (typeof value === "string" && value.length <= 16384) result[key] = value;
        }
        return result;
      };
      return {
        origin: window.location.origin,
        local: select(window.localStorage),
        session: select(window.sessionStorage),
      };
    });
  } catch {
    return null;
  }
}

function isMakerWorldModelUrl(value, platform) {
  try {
    const parsed = new URL(String(value || ""));
    const hostname = parsed.hostname.toLowerCase();
    const domain = platform === "global" ? "makerworld.com" : "makerworld.com.cn";
    return parsed.protocol === "https:"
      && (hostname === domain || hostname.endsWith(`.${domain}`))
      && /\/models\/\d+/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function platformDomains(platform) {
  return platform === "global"
    ? ["makerworld.com", "bambulab.com"]
    : ["makerworld.com.cn", "bambulab.cn"];
}

function hostnameMatchesDomains(hostname, domains) {
  const cleanHostname = String(hostname || "").trim().toLowerCase().replace(/^\.+/, "");
  return domains.some((domain) => cleanHostname === domain || cleanHostname.endsWith(`.${domain}`));
}

function isAllowedBrowserFetchUrl(value, platform) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:"
      && !parsed.username
      && !parsed.password
      && (!parsed.port || parsed.port === "443")
      && hostnameMatchesDomains(parsed.hostname, platformDomains(platform));
  } catch {
    return false;
  }
}

const ALLOWED_FETCH_HEADERS = new Set([
  "accept",
  "accept-language",
  "authorization",
  "origin",
  "referer",
  "token",
  "x-access-token",
  "x-app-name",
  "x-app-version",
  "x-bbl-app-source",
  "x-bbl-client-type",
  "x-bbl-client-name",
  "x-bbl-client-version",
  "x-bbl-captcha-result",
  "x-token",
]);

function cleanFetchHeaders(headers) {
  const source = headers && typeof headers === "object" ? headers : {};
  const result = {};
  for (const [name, value] of Object.entries(source)) {
    const cleanName = String(name || "").trim();
    if (!cleanName || !ALLOWED_FETCH_HEADERS.has(cleanName.toLowerCase()) || value == null) continue;
    result[cleanName.toLowerCase()] = String(value);
  }
  return result;
}

function headerExists(headers, targetName) {
  const normalizedTarget = String(targetName || "").toLowerCase();
  return Object.keys(headers).some((name) => name.toLowerCase() === normalizedTarget);
}

function isControlApiUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.hostname.toLowerCase().startsWith("api.bambulab.")
      || parsed.pathname.startsWith("/api/")
      || parsed.pathname.startsWith("/v1/");
  } catch {
    return false;
  }
}

function headersWithBrowserAuth(headers, cookies, targetUrl) {
  const result = { ...headers };
  if (!isControlApiUrl(targetUrl)) return result;
  const token = browserAuthTokenForUrl(cookies, targetUrl);
  if (!token) return result;
  if (!headerExists(result, "authorization")) result.authorization = `Bearer ${token}`;
  if (!headerExists(result, "token")) result.token = token;
  if (!headerExists(result, "x-token")) result["x-token"] = token;
  if (!headerExists(result, "x-access-token")) result["x-access-token"] = token;
  return result;
}

function isMakerHubApiTargetUrl(value, platform) {
  try {
    const parsed = new URL(String(value || ""));
    const apiHost = platform === "global" ? "api.bambulab.com" : "api.bambulab.cn";
    return parsed.protocol === "https:"
      && parsed.hostname.toLowerCase() === apiHost
      && (parsed.pathname.startsWith("/api/") || parsed.pathname.startsWith("/v1/"));
  } catch {
    return false;
  }
}

async function cleanupStaleAutomationTargets(browser, context, platform) {
  const client = await browser.target().createCDPSession();
  try {
    const { targetInfos = [] } = await client.send("Target.getTargets");
    for (const targetInfo of targetInfos) {
      if (
        targetInfo.type !== "page"
        || String(targetInfo.browserContextId || "") !== String(context.id || "")
        || !isMakerHubApiTargetUrl(targetInfo.url, platform)
      ) continue;
      await client.send("Target.closeTarget", { targetId: targetInfo.targetId }).catch(() => undefined);
    }
  } catch {
    // 历史标签清理失败不应阻断新的浏览器操作。
  } finally {
    await client.detach().catch(() => undefined);
  }
}

async function withTemporaryTarget(browser, context, { hidden }, callback) {
  const client = await browser.target().createCDPSession();
  const markerUrl = `about:blank#makerhub-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let targetId = "";
  try {
    const targetOptions = {
      url: markerUrl,
      browserContextId: context.id || undefined,
      background: true,
    };
    if (hidden) targetOptions.hidden = true;
    ({ targetId } = await client.send("Target.createTarget", targetOptions));
    const target = await browser.waitForTarget(
      (candidate) => candidate.url() === markerUrl && candidate.browserContext() === context,
      { timeout: 15000 },
    );
    return await callback(target);
  } finally {
    if (targetId) {
      await client.send("Target.closeTarget", { targetId }).catch(() => undefined);
    }
    await client.detach().catch(() => undefined);
  }
}

async function withTemporaryCdpSession(browser, context, callback) {
  return await withTemporaryTarget(browser, context, { hidden: true }, async (target) => {
    const session = await target.createCDPSession();
    try {
      return await callback(session);
    } finally {
      await session.detach().catch(() => undefined);
    }
  });
}

async function withTemporaryPage(browser, context, callback) {
  return await withTemporaryTarget(browser, context, { hidden: false }, async (target) => {
    const page = await target.page();
    if (!page) throw new Error("temporary browser target did not expose a page");
    return await callback(page);
  });
}

function createCdpEventRecorder(session, eventName, predicate) {
  const events = [];
  const handler = (event) => {
    if (predicate(event)) events.push(event);
  };
  session.on(eventName, handler);
  return {
    async wait(timeoutMs, timeoutMessage) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        if (events.length) return events.shift();
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      throw new Error(timeoutMessage);
    },
    stop() {
      session.off(eventName, handler);
    },
  };
}

async function waitForCdpCondition(predicate, timeoutMs, timeoutMessage) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(timeoutMessage);
}

function isCloudflareChallengeResponse(response) {
  if (Number(response?.status_code || 0) !== 403) return false;
  const text = String(response?.text || "").toLowerCase();
  return [
    "cf-chl-",
    "challenge-platform",
    "<title>just a moment",
    "<title>请稍候",
  ].some((marker) => text.includes(marker));
}

async function fetchVisibleBrowserResponse(browser, context, platform, targetUrl, timeoutMs) {
  return await withTemporaryPage(browser, context, async (page) => {
    const response = await page.goto(targetUrl, {
      waitUntil: "domcontentloaded",
      timeout: Math.max(Number(timeoutMs || 30000), 15000),
    });
    if (!response) throw new Error("browser fallback fetch did not return a response");
    const finalUrl = page.url();
    if (!isAllowedBrowserFetchUrl(finalUrl, platform)) {
      throw new Error("browser fallback fetch redirected outside allowed domains");
    }
    const responseHeaders = response.headers();
    const safeHeaders = {};
    for (const name of ["content-type", "retry-after", "location"]) {
      const value = cdpHeaderValue(responseHeaders, name);
      if (value) safeHeaders[name] = value;
    }
    return {
      status_code: response.status(),
      url: finalUrl,
      content_type: cdpHeaderValue(responseHeaders, "content-type"),
      headers: safeHeaders,
      text: await response.text(),
    };
  });
}

function cdpHeaderEntries(headers) {
  return Object.entries(headers).map(([name, value]) => ({
    name: String(name),
    value: String(value),
  }));
}

function cdpHeaderValue(headers, targetName) {
  const cleanTarget = String(targetName || "").toLowerCase();
  const entry = Object.entries(headers || {}).find(([name]) => name.toLowerCase() === cleanTarget);
  return entry ? String(entry[1]) : "";
}

async function fetchBrowserResponse(browser, context, platform, targetUrl, headers, cookies, timeoutMs) {
  if (!isAllowedBrowserFetchUrl(targetUrl, platform)) throw new Error("invalid browser fetch URL");
  const cleanCookies = (Array.isArray(cookies) ? cookies : []).map(cleanCookie).filter(Boolean);
  if (cleanCookies.length) await context.setCookie(...cleanCookies);
  const result = await withTemporaryCdpSession(browser, context, async (session) => {
    const profileCookies = (await context.cookies()).filter((item) => (
      hostnameMatchesDomains(String(item?.domain || ""), platformDomains(platform))
    ));
    const cleanHeaders = headersWithBrowserAuth(cleanFetchHeaders(headers), profileCookies, targetUrl);
    const navigationTimeout = Math.max(Number(timeoutMs || 30000), 15000);
    const finishedRequests = new Set();
    const failedRequests = new Map();
    const interceptionTasks = new Set();
    const responseRecorder = createCdpEventRecorder(
      session,
      "Network.responseReceived",
      (event) => event.type === "Document",
    );
    const onLoadingFinished = (event) => finishedRequests.add(event.requestId);
    const onLoadingFailed = (event) => failedRequests.set(event.requestId, event.errorText || "request failed");
    const onRequestPaused = (event) => {
      const task = (async () => {
        if (
          event.resourceType !== "Document"
          || !isAllowedBrowserFetchUrl(event.request.url, platform)
        ) {
          await session.send("Fetch.failRequest", {
            requestId: event.requestId,
            errorReason: "BlockedByClient",
          });
          return;
        }
        await session.send("Fetch.continueRequest", {
          requestId: event.requestId,
          headers: cdpHeaderEntries({ ...event.request.headers, ...cleanHeaders }),
        });
      })();
      interceptionTasks.add(task);
      void task.catch(() => undefined).finally(() => interceptionTasks.delete(task));
    };
    session.on("Network.loadingFinished", onLoadingFinished);
    session.on("Network.loadingFailed", onLoadingFailed);
    session.on("Fetch.requestPaused", onRequestPaused);
    try {
      await session.send("Page.enable");
      await session.send("Network.enable", {
        maxTotalBufferSize: BROWSER_FETCH_TOTAL_BUFFER_BYTES,
        maxResourceBufferSize: BROWSER_FETCH_RESOURCE_BUFFER_BYTES,
        enableDurableMessages: true,
      });
      await session.send("Fetch.enable", {
        patterns: [{ urlPattern: "*", requestStage: "Request" }],
      });
      const navigation = await session.send("Page.navigate", { url: targetUrl });
      if (navigation.errorText) throw new Error(`browser fetch navigation failed: ${navigation.errorText}`);
      const responseEvent = await responseRecorder.wait(
        navigationTimeout,
        "browser fetch did not return a response",
      );
      const requestId = responseEvent.requestId;
      const finalUrl = String(responseEvent.response?.url || targetUrl);
      if (!isAllowedBrowserFetchUrl(finalUrl, platform)) {
        throw new Error("browser fetch redirected outside allowed domains");
      }
      await waitForCdpCondition(
        () => finishedRequests.has(requestId) || failedRequests.has(requestId),
        navigationTimeout,
        "browser fetch response body timed out",
      );
      if (failedRequests.has(requestId)) {
        throw new Error(`browser fetch request failed: ${failedRequests.get(requestId)}`);
      }
      const body = await session.send("Network.getResponseBody", { requestId });
      const responseHeaders = responseEvent.response?.headers || {};
      const safeHeaders = {};
      for (const name of ["content-type", "retry-after", "location"]) {
        const value = cdpHeaderValue(responseHeaders, name);
        if (value) safeHeaders[name] = value;
      }
      return {
        status_code: Number(responseEvent.response?.status || 0),
        url: finalUrl,
        content_type: cdpHeaderValue(responseHeaders, "content-type")
          || String(responseEvent.response?.mimeType || ""),
        headers: safeHeaders,
        text: body.base64Encoded
          ? Buffer.from(String(body.body || ""), "base64").toString("utf8")
          : String(body.body || ""),
      };
    } finally {
      responseRecorder.stop();
      session.off("Network.loadingFinished", onLoadingFinished);
      session.off("Network.loadingFailed", onLoadingFailed);
      session.off("Fetch.requestPaused", onRequestPaused);
      await Promise.allSettled([...interceptionTasks]);
      await session.send("Fetch.disable").catch(() => undefined);
    }
  });
  if (isMakerWorldUrl(targetUrl, platform) && isCloudflareChallengeResponse(result)) {
    return await fetchVisibleBrowserResponse(browser, context, platform, targetUrl, timeoutMs);
  }
  return result;
}

function isThreeMfAuthorizationUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    const allowedHosts = new Set([
      "api.bambulab.com",
      "api.bambulab.cn",
      "makerworld.com",
      "makerworld.com.cn",
    ]);
    return (
      parsed.protocol === "https:"
      && allowedHosts.has(parsed.hostname.toLowerCase())
      && /^\/(?:api\/)?v1\/design-service\/instance\/\d+\/f3mf\/?$/.test(parsed.pathname)
    );
  } catch {
    return false;
  }
}

function sanitizedAuthorizationPayload(payload, text) {
  const body = payload && typeof payload === "object" ? payload : {};
  const data = body.data && typeof body.data === "object"
    ? body.data
    : body.result && typeof body.result === "object"
      ? body.result
      : body;
  const name = String(data.name || data.fileName || data.filename || data.file_name || "").trim();
  const url = String(data.url || data.downloadUrl || data.download_url || data.downloadURL || "").trim();
  if (url) return { name, url };
  return {
    message: String(body.message || body.error || body.msg || text || "").slice(0, 1024),
    code: String(body.code || "").slice(0, 80),
    captchaId: String(body.captchaId || body.captcha_id || "").slice(0, 160),
  };
}

function authorizationResponseMatches(response, instanceId) {
  if (!isThreeMfAuthorizationUrl(response.url())) return false;
  const matched = response.url().match(/\/instance\/(\d+)\/f3mf/i);
  return !instanceId || matched?.[1] === String(instanceId);
}

export async function readAuthorizationResponse(response) {
  let text;
  try {
    text = String(await response.text()).slice(0, 16384);
  } catch {
    throw new Error("authorization response body unavailable");
  }
  let statusCode;
  try {
    statusCode = Number(response.status() || 0);
  } catch {
    throw new Error("authorization response metadata unavailable");
  }
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  return {
    status_code: statusCode,
    payload: sanitizedAuthorizationPayload(payload, text),
    text: payload ? "" : text.slice(0, 1024),
  };
}

function isVerificationAuthorization(response) {
  return response.status_code === 418 || Boolean(response.payload?.captchaId);
}

function isSuccessfulAuthorization(response) {
  return response.status_code >= 200
    && response.status_code < 300
    && Boolean(String(response.payload?.url || "").trim());
}

async function settlePageGeneratedAuthorization(responseOutcome, originalResponse) {
  const outcome = await responseOutcome;
  if (!outcome.response) return originalResponse;
  try {
    return await readAuthorizationResponse(outcome.response);
  } catch {
    return originalResponse;
  }
}

function authorizationResponseOutcome(page, matcher, options) {
  try {
    return Promise.resolve(page.waitForResponse(matcher, options)).then(
      (response) => ({ response, error: null }),
      (error) => ({ response: null, error }),
    );
  } catch (error) {
    return Promise.resolve({ response: null, error });
  }
}

function authorizationWaitError(error) {
  const timedOut = error?.name === "TimeoutError" || /timed?\s*out/i.test(String(error?.message || ""));
  return new Error(timedOut
    ? "3MF authorization response timed out"
    : "3MF authorization response unavailable");
}

export function threeMfDownloadActionScore(candidate = {}) {
  if (!candidate.visible || candidate.disabled) return 0;
  const ownText = [
    candidate.text,
    candidate.ariaLabel,
    candidate.title,
    candidate.testId,
    candidate.tracking,
  ].map((value) => String(value || "")).join(" ").replace(/\s+/g, " ").trim();
  const signalText = [ownText, candidate.className, candidate.href]
    .map((value) => String(value || ""))
    .join(" ");
  const contextText = String(candidate.contextText || "").replace(/\s+/g, " ").trim();
  const hasDownloadSignal = /(?:下载|download)/i.test(signalText);
  const hasModelSignal = /(?:3\s*mf|打印配置|print(?:ing)?\s*profile|模型|model)/i.test(signalText);
  const contextHasThreeMf = /3\s*mf/i.test(contextText);
  const primarySignal = /(?:primary|download|3mf)/i.test(
    [candidate.className, candidate.testId, candidate.tracking].join(" "),
  );
  if (hasDownloadSignal && hasModelSignal) return 100;
  if (hasDownloadSignal && contextHasThreeMf) return 80;
  if (
    primarySignal
    && /^(?:下载|download)(?:\s*(?:文件|file|模型|model|打印配置|print(?:ing)?\s*profile))?$/i.test(ownText)
  ) return 70;
  return 0;
}

export async function coordinateThreeMfAuthorization(page, options = {}) {
  const authorizationTimeout = Math.max(Number(options.authorizationTimeout || 90000), 1);
  const navigationTimeout = Math.max(Number(options.navigationTimeout || 30000), 1);
  const matcher = (response) => authorizationResponseMatches(response, options.instanceId);
  const firstWaiterController = new AbortController();
  const firstResponseOutcome = authorizationResponseOutcome(page, matcher, {
    timeout: authorizationTimeout,
    signal: firstWaiterController.signal,
  });
  try {
    const findButton = options.findButton || findThreeMfDownloadButton;
    const button = await findButton(page, navigationTimeout);
    try {
      await button.click({ delay: 20 });
    } finally {
      try {
        await button.dispose();
      } catch {}
    }
    const firstOutcome = await firstResponseOutcome;
    if (!firstOutcome.response) throw authorizationWaitError(firstOutcome.error);
    const secondWaiterController = options.inputAutoVerify ? new AbortController() : null;
    const secondResponseOutcome = secondWaiterController
      ? authorizationResponseOutcome(page, matcher, {
        timeout: AUTO_VERIFY_TIMEOUT_MS,
        signal: secondWaiterController.signal,
      })
      : null;
    const secondParsedResponse = secondResponseOutcome
      ? settlePageGeneratedAuthorization(secondResponseOutcome, null)
      : Promise.resolve(null);
    const secondAuthorization = secondParsedResponse.then((response) => (
      isSuccessfulAuthorization(response || {}) ? response : null
    ));
    try {
      const first = await readAuthorizationResponse(firstOutcome.response);
      if (!options.inputAutoVerify || !isVerificationAuthorization(first)) {
        return { ...first, navigation_timed_out: Boolean(options.navigationTimedOut) };
      }

      const verificationAdapter = options.verificationAdapter || attemptAutomaticVerification;
      const verificationController = new AbortController();
      const verificationOutcome = Promise.resolve().then(async () => {
        try {
          return sanitizeVerificationResult(await verificationAdapter(page, {
            timeoutMs: AUTO_VERIFY_TIMEOUT_MS,
            signal: verificationController.signal,
          }));
        } catch {
          return sanitizeVerificationResult({
            attempted: true,
            completed: false,
            reason: "verification_failed",
          });
        }
      });
      const pageSuccess = secondAuthorization.then((response) => (
        response
          ? { source: "page", response }
          : new Promise(() => {})
      ));
      const winner = await Promise.race([
        pageSuccess,
        verificationOutcome.then((verification) => ({ source: "adapter", verification })),
      ]);

      let verification;
      let finalResponse;
      if (winner.source === "page") {
        verificationController.abort(new Error("authorization completed"));
        const adapterResult = await verificationOutcome;
        verification = sanitizeVerificationResult({
          ...adapterResult,
          completed: true,
          reason: "completed",
        });
        finalResponse = winner.response;
      } else {
        verification = winner.verification;
        finalResponse = verification.completed
          ? (await secondParsedResponse) || first
          : first;
      }
      return {
        ...finalResponse,
        navigation_timed_out: Boolean(options.navigationTimedOut),
        verification,
      };
    } finally {
      secondWaiterController?.abort();
      if (secondResponseOutcome) await secondResponseOutcome;
    }
  } finally {
    firstWaiterController.abort();
    await firstResponseOutcome;
  }
}

async function findThreeMfDownloadButton(page, timeoutMs) {
  const deadline = Date.now() + Math.max(Number(timeoutMs || 30000), 15000);
  while (Date.now() < deadline) {
    const handles = await page.$$(
      "button, a, [role='button'], .primaryButton, [aria-label*='download' i], "
      + "[title*='download' i], [data-testid*='download' i], [class*='download' i]",
    );
    let bestHandle = null;
    let bestScore = 0;
    for (const handle of handles) {
      let candidate;
      try {
        candidate = await handle.evaluate((element) => {
          const style = window.getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          const contextParts = [];
          let current = element;
          for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
            const value = String(current.innerText || current.textContent || "").replace(/\s+/g, " ").trim();
            if (value) contextParts.push(value.slice(0, 512));
          }
          return {
            visible: style.display !== "none"
              && style.visibility !== "hidden"
              && style.opacity !== "0"
              && style.pointerEvents !== "none"
              && rect.width > 0
              && rect.height > 0,
            disabled: element.hasAttribute("disabled")
              || element.getAttribute("aria-disabled") === "true"
              || /(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/i.test(String(element.className || "")),
            text: String(element.innerText || element.textContent || "").slice(0, 256),
            ariaLabel: String(element.getAttribute("aria-label") || "").slice(0, 256),
            title: String(element.getAttribute("title") || "").slice(0, 256),
            testId: String(element.getAttribute("data-testid") || "").slice(0, 256),
            tracking: String(
              element.getAttribute("data-track")
              || element.getAttribute("data-event")
              || element.getAttribute("data-action")
              || "",
            ).slice(0, 256),
            className: String(element.className || "").slice(0, 256),
            href: String(element.getAttribute("href") || "").slice(0, 512),
            contextText: contextParts.join(" ").slice(0, 1536),
          };
        });
      } catch {
        await handle.dispose().catch(() => undefined);
        continue;
      }
      const score = threeMfDownloadActionScore(candidate);
      if (score > bestScore) {
        await bestHandle?.dispose().catch(() => undefined);
        bestHandle = handle;
        bestScore = score;
      } else {
        await handle.dispose().catch(() => undefined);
      }
    }
    if (bestHandle) return bestHandle;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("model page did not expose an enabled 3MF download action");
}

async function clickAuthorization(
  browser,
  context,
  platform,
  targetUrl,
  modelUrl,
  instanceId,
  navigationTimeoutMs,
  authorizationTimeoutMs,
  inputAutoVerify,
) {
  if (!isThreeMfAuthorizationUrl(targetUrl)) throw new Error("invalid 3MF authorization URL");
  if (!isMakerWorldModelUrl(modelUrl, platform)) throw new Error("invalid MakerWorld model page URL");
  const navigationTimeout = Math.max(Number(navigationTimeoutMs || 30000), 15000);
  const authorizationTimeout = Math.max(Number(authorizationTimeoutMs || 90000), navigationTimeout);
  return await withTemporaryPage(browser, context, async (page) => {
    let navigationTimedOut = false;
    try {
      await page.goto(modelUrl, { waitUntil: "domcontentloaded", timeout: navigationTimeout });
    } catch (error) {
      if (!(error instanceof Error) || error.name !== "TimeoutError") throw error;
      navigationTimedOut = true;
    }
    const authorization = await coordinateThreeMfAuthorization(page, {
      instanceId,
      navigationTimeout,
      authorizationTimeout,
      navigationTimedOut,
      inputAutoVerify,
      findButton: async () => {
        const button = await findThreeMfDownloadButton(page, navigationTimeout);
        return button;
      },
    });
    return { ...authorization, navigation_timed_out: navigationTimedOut };
  });
}

async function main() {
  const input = await readInput();
  const cdpUrl = String(input.cdp_url || "").trim();
  if (!cdpUrl) throw new Error("cdp_url is required");
  const headers = authHeaders(String(input.auth_token || "").trim());
  const browserWSEndpoint = await resolveWebSocketEndpoint(cdpUrl, headers);
  const browser = await puppeteer.connect({
    browserWSEndpoint,
    headers,
    defaultViewport: null,
    protocolTimeout: Math.max(
      Number(input.navigation_timeout_ms || 30000),
      input.action === "click" ? Number(input.authorization_timeout_ms || 90000) : 15000,
    ),
  });
  let navigationError = "";
  try {
    const contexts = browser.browserContexts();
    const context = contexts[0] || browser.defaultBrowserContext();
    await cleanupStaleAutomationTargets(browser, context, input.platform);
    if (input.action === "fetch") {
      const fetched = await fetchBrowserResponse(
        browser,
        context,
        String(input.platform || "cn"),
        String(input.target_url || ""),
        input.headers,
        input.cookies,
        input.navigation_timeout_ms,
      );
      process.stdout.write(JSON.stringify({ ok: true, ...fetched }));
      return;
    }
    if (input.action === "click") {
      const authorization = await clickAuthorization(
        browser,
        context,
        String(input.platform || "cn"),
        String(input.target_url || ""),
        String(input.model_url || ""),
        String(input.instance_id || ""),
        input.navigation_timeout_ms,
        input.authorization_timeout_ms,
        input.auto_verify_3mf === true,
      );
      process.stdout.write(JSON.stringify({ ok: true, ...authorization }));
      return;
    }
    if (input.action === "seed") {
      const cookies = (Array.isArray(input.cookies) ? input.cookies : []).map(cleanCookie).filter(Boolean);
      if (cookies.length) await context.setCookie(...cookies);
    }
    const pages = await context.pages();
    const page = pages[0] || await context.newPage();
    const platform = normalizePlatform(input.platform);
    const isLoginAction = input.action === "login" || input.action === "sync";
    const makerWorldSessionReady = isLoginAction
      && hasMakerWorldSessionCookie(await context.cookies(), platform);
    const targetUrl = makerWorldSessionReady
      ? makerWorldHomeUrl(platform)
      : String(input.target_url || "");
    const currentUrl = page.url();
    const shouldNavigate = targetUrl && (
      input.action === "seed"
      || input.action === "login"
      || !/^https?:\/\//i.test(currentUrl)
      || (makerWorldSessionReady && !isMakerWorldUrl(currentUrl, platform))
    );
    if (shouldNavigate) {
      try {
        await page.goto(targetUrl, {
          waitUntil: "domcontentloaded",
          timeout: Math.max(Number(input.navigation_timeout_ms || 30000), 15000),
        });
        await new Promise((resolve) => setTimeout(resolve, 1000));
      } catch (error) {
        navigationError = error instanceof Error ? error.message : String(error || "navigation failed");
      }
    }
    if (isLoginAction && isBambuLoginConfirmationUrl(page.url(), platform)) {
      await completeBambuLoginConfirmation(
        context,
        page,
        platform,
        input.navigation_timeout_ms,
      );
    }
    const currentPages = await context.pages();
    const storage = [];
    for (const currentPage of currentPages) {
      const item = await storageSnapshot(currentPage);
      if (item) storage.push(item);
    }
    process.stdout.write(JSON.stringify({
      ok: true,
      current_url: page.url(),
      cookies: await context.cookies(),
      storage,
      navigation_error: navigationError,
    }));
  } finally {
    await browser.disconnect();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write((error instanceof Error ? error.message : String(error || "CDP bridge failed")) + "\n");
    process.exit(1);
  });
}
