const MAX_RESPONSE_BYTES = 24 * 1024 * 1024;
const SAFE_RESPONSE_HEADERS = ["content-type", "retry-after", "location", "cf-mitigated"];

function positiveMilliseconds(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : fallback;
}

function headerValue(headers, name) {
  const entry = Object.entries(headers || {}).find(([key]) => key.toLowerCase() === name);
  return entry ? String(entry[1]) : "";
}

function safeResponseHeaders(headers) {
  return Object.fromEntries(SAFE_RESPONSE_HEADERS.map((name) => [name, headerValue(headers, name)])
    .filter(([, value]) => value));
}

function isCloudflareResponse(response) {
  const headers = response.headers || {};
  const text = String(response.text || "").toLowerCase();
  const html = /(?:text\/html|application\/xhtml)/i.test(response.content_type || "")
    || /^\s*(?:<!doctype\s+html|<html|<title)/i.test(text);
  if (!html) return false;
  if (headerValue(headers, "cf-mitigated").toLowerCase() === "challenge") return true;
  return [200, 403, 503].includes(Number(response.status_code)) && (
    /(?:cf-chl-|\/cdn-cgi\/challenge-platform\/)/i.test(text)
    || /<title>\s*(?:just a moment|请稍候)/i.test(text)
  );
}

// 这里只接收任务自己创建的标签页；标签归属登记和关闭由调用者的 helper 保证。
export async function fetchInBusinessPage(input, dependencies) {
  const {
    browser, context, platform, targetUrl: rawTargetUrl, headers, cookies,
    allowInteraction = false,
  } = input;
  const {
    withTemporaryPage, isAllowedBrowserFetchUrl, cleanCookie, cleanFetchHeaders,
    headersWithBrowserAuth, hostnameMatchesDomains, platformDomains, makerWorldHomeUrl,
    inspectPageVerificationState, ensurePageVerification,
  } = dependencies;
  if (!isAllowedBrowserFetchUrl(rawTargetUrl, platform)) throw new Error("invalid browser fetch URL");
  const normalizedTarget = new URL(rawTargetUrl);
  normalizedTarget.hash = "";
  const targetUrl = normalizedTarget.href;
  const businessUrl = makerWorldHomeUrl(platform);
  if (!isAllowedBrowserFetchUrl(businessUrl, platform)) throw new Error("invalid browser business URL");
  const requestTimeout = positiveMilliseconds(input.timeoutMs, 30000);
  const verificationTimeout = Math.min(50000, positiveMilliseconds(input.verificationTimeoutMs, 50000));
  const defaultTotalTimeout = requestTimeout + Math.min(verificationTimeout, allowInteraction ? 50000 : 10000);
  const deadline = performance.now() + positiveMilliseconds(input.totalTimeoutMs, defaultTotalTimeout);
  const maxBytes = Math.min(MAX_RESPONSE_BYTES, positiveMilliseconds(input.maxResponseBytes, MAX_RESPONSE_BYTES));
  const retrySafe = !/^\/(?:api\/)?v1\/design-service\/instance\/\d+\/f3mf\/?$/i
    .test(new URL(targetUrl).pathname);
  const maxVerificationAttempts = Math.min(2, positiveMilliseconds(input.verificationMaxAttempts, 2));
  let verificationRemaining = verificationTimeout;
  let verificationAttempts = 0;
  let interactionAttempts = 0;
  let verification;

  const remaining = () => {
    const milliseconds = Math.floor(deadline - performance.now());
    if (milliseconds <= 0) throw new Error("browser fetch timed out");
    return milliseconds;
  };
  const withinBudget = async (operation, limit = remaining()) => {
    let timer;
    const milliseconds = Math.min(limit, remaining());
    try {
      return await Promise.race([
        Promise.resolve().then(operation),
        new Promise((_resolve, reject) => {
          timer = setTimeout(() => reject(new Error("browser fetch timed out")), milliseconds);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
  const domains = platformDomains(platform);
  const allowedCookies = (Array.isArray(cookies) ? cookies : []).map(cleanCookie).filter((cookie) => {
    if (!cookie) return false;
    return cookie.domain
      ? hostnameMatchesDomains(cookie.domain, domains)
      : isAllowedBrowserFetchUrl(cookie.url, platform);
  });
  if (allowedCookies.length) await withinBudget(() => context.setCookie(...allowedCookies));

  let releaseSession = async () => {};
  return await withTemporaryPage(browser, context, async (page) => {
    const session = await withinBudget(() => page.target().createCDPSession());
    let mainFrameId = "";
    let requestActive = false;
    let blockedRequest = false;
    let interceptionError = false;
    let challengeHint;
    const fetchIds = new Set();
    const networkIds = new Set();
    const interceptionTasks = new Set();
    const onRequestPaused = (event) => {
      const task = (async () => {
        const apiChain = requestActive && (
          event.request.url === targetUrl
          || fetchIds.has(event.redirectedRequestId)
          || (event.networkId && networkIds.has(event.networkId))
        );
        if (apiChain) {
          fetchIds.add(event.requestId);
          if (event.networkId) networkIds.add(event.networkId);
        }
        const mainNavigation = event.resourceType === "Document" && event.frameId === mainFrameId;
        const managedFetch = requestActive && ["Fetch", "XHR"].includes(event.resourceType);
        const redirectLocation = (event.responseHeaders || [])
          .find((header) => header.name.toLowerCase() === "location")?.value;
        const invalidRedirect = redirectLocation && Number(event.responseStatusCode) >= 300
          && Number(event.responseStatusCode) < 400
          && !isAllowedBrowserFetchUrl(new URL(redirectLocation, event.request.url).href, platform);
        if ((apiChain || mainNavigation || managedFetch) && (
          !isAllowedBrowserFetchUrl(event.request.url, platform) || invalidRedirect
        )) {
          blockedRequest = true;
          await session.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "BlockedByClient" });
          return;
        }
        if (apiChain && event.responseStatusCode) {
          const responseHeaders = Object.fromEntries((event.responseHeaders || []).map(({ name, value }) => [name, value]));
          const hint = {
            status_code: event.responseStatusCode, url: event.request.url,
            content_type: headerValue(responseHeaders, "content-type"),
            headers: safeResponseHeaders(responseHeaders), text: "",
          };
          // CORS 可阻止页面读取 CF 的正文；只以明确的响应标头识别验证，不旁路读取业务数据。
          if (isCloudflareResponse(hint)) challengeHint = hint;
        }
        await session.send("Fetch.continueRequest", { requestId: event.requestId });
      })();
      interceptionTasks.add(task);
      void task.catch(() => { interceptionError = true; }).finally(() => interceptionTasks.delete(task));
    };
    const assertAllowedPage = () => {
      if (blockedRequest || !isAllowedBrowserFetchUrl(page.url(), platform)) {
        throw new Error("browser fetch redirected outside allowed domains");
      }
      if (interceptionError) throw new Error("browser fetch request interception failed");
    };
    const navigate = async (url) => {
      if (!isAllowedBrowserFetchUrl(url, platform)) throw new Error("invalid browser business URL");
      const response = await withinBudget(() => page.goto(url, {
        waitUntil: "domcontentloaded",
        timeout: Math.min(requestTimeout, remaining()),
      }));
      assertAllowedPage();
      return response;
    };
    const verifyPage = async () => {
      assertAllowedPage();
      const state = await withinBudget(() => inspectPageVerificationState(page));
      if (!state.detected) return true;
      if (verificationAttempts >= maxVerificationAttempts || verificationRemaining <= 0) {
        verification = { detected: true, completed: false, manual_required: true, kind: state.kind, reason: "verification_budget_exhausted" };
        return false;
      }
      const budget = Math.min(verificationRemaining, remaining());
      const started = performance.now();
      verificationAttempts += 1;
      try {
        const interactionsRemaining = Math.max(0, maxVerificationAttempts - interactionAttempts);
        verification = await withinBudget(() => ensurePageVerification(page, {
          timeoutMs: budget,
          passiveWaitMs: Math.min(5000, budget),
          allowInteraction: allowInteraction && interactionsRemaining > 0,
          maxAttempts: Math.max(1, interactionsRemaining),
        }), budget);
        const attempts = Number(verification.verification?.attempts);
        interactionAttempts += Number.isFinite(attempts) ? Math.max(0, attempts) : (allowInteraction ? 1 : 0);
      } finally {
        verificationRemaining -= performance.now() - started;
      }
      assertAllowedPage();
      return verification.completed === true;
    };
    const navigationResponse = async (response) => {
      const text = await withinBudget(() => page.content());
      if (Buffer.byteLength(text, "utf8") > maxBytes) throw new Error("browser fetch response body exceeds size limit");
      const responseHeaders = response?.headers() || {};
      return {
        status_code: response?.status() || 403,
        url: page.url(),
        content_type: headerValue(responseHeaders, "content-type") || "text/html",
        headers: safeResponseHeaders(responseHeaders),
        text,
        verification,
      };
    };
    const fetchOnce = async () => {
      assertAllowedPage();
      const profileCookies = (await withinBudget(() => context.cookies())).filter((cookie) => (
        hostnameMatchesDomains(String(cookie?.domain || ""), domains)
      ));
      const requestHeaders = headersWithBrowserAuth(cleanFetchHeaders(headers), profileCookies, targetUrl);
      // Origin、Referer 和 Cookie 由浏览器自身生成，不能让调用者覆盖浏览器的来源语义。
      for (const name of Object.keys(requestHeaders)) {
        if (["origin", "referer", "cookie", "host"].includes(name.toLowerCase())) delete requestHeaders[name];
      }
      requestActive = true;
      challengeHint = undefined;
      let result;
      try {
        result = await withinBudget(() => page.evaluate(async (options) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), options.timeoutMs);
          try {
            if (location.origin !== options.pageOrigin) return { transport_error: "page_changed" };
            const response = await fetch(options.url, {
              method: "GET", headers: options.headers, credentials: "include", cache: "no-store",
              redirect: "follow", signal: controller.signal,
            });
            if (Number(response.headers.get("content-length")) > options.maxBytes) {
              await response.body?.cancel();
              return { transport_error: "body_too_large" };
            }
            const reader = response.body?.getReader();
            const decoder = new TextDecoder();
            const chunks = [];
            let size = 0;
            if (reader) {
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                size += value.byteLength;
                if (size > options.maxBytes) {
                  await reader.cancel();
                  return { transport_error: "body_too_large" };
                }
                chunks.push(decoder.decode(value, { stream: true }));
              }
              chunks.push(decoder.decode());
            }
            const responseHeaders = {};
            for (const name of ["content-type", "retry-after", "location", "cf-mitigated"]) {
              const value = response.headers.get(name);
              if (value) responseHeaders[name] = value;
            }
            return {
              status_code: response.status, url: response.url,
              content_type: response.headers.get("content-type") || "",
              headers: responseHeaders, text: chunks.join(""),
            };
          } catch {
            return { transport_error: controller.signal.aborted ? "timeout" : "network_error" };
          } finally {
            clearTimeout(timer);
          }
        }, {
          url: targetUrl, headers: requestHeaders, pageOrigin: new URL(page.url()).origin,
          timeoutMs: Math.min(requestTimeout, remaining()), maxBytes,
        }));
      } finally {
        requestActive = false;
      }
      assertAllowedPage();
      if (result.transport_error === "network_error" && challengeHint) return challengeHint;
      if (result.transport_error === "body_too_large") throw new Error("browser fetch response body exceeds size limit");
      if (result.transport_error === "timeout") throw new Error("browser fetch timed out");
      if (result.transport_error) throw new Error("browser fetch network request failed (check browser CORS/proxy)");
      if (!isAllowedBrowserFetchUrl(result.url, platform)) throw new Error("browser fetch redirected outside allowed domains");
      return result;
    };
    session.on("Fetch.requestPaused", onRequestPaused);
    // 标签先关闭，最后再释放拦截器，避免超时关闭过程中出现不受检查的重定向窗口。
    releaseSession = async () => {
      session.off("Fetch.requestPaused", onRequestPaused);
      await Promise.allSettled([...interceptionTasks]);
      await session.send("Fetch.disable").catch(() => undefined);
      await session.detach().catch(() => undefined);
    };
    await withinBudget(() => session.send("Page.enable"));
    ({ frameTree: { frame: { id: mainFrameId } } } = await withinBudget(() => session.send("Page.getFrameTree")));
    await withinBudget(() => session.send("Fetch.enable", { patterns:
      ["Document", "Fetch", "XHR"].flatMap((resourceType) => ["Request", "Response"]
        .map((requestStage) => ({ urlPattern: "*", resourceType, requestStage }))),
    }));
    const navigation = await navigate(businessUrl);
    if (!await verifyPage()) return await navigationResponse(navigation);
    let result = await fetchOnce();
    if (retrySafe && isCloudflareResponse(result)) {
      // 不再次导航目标 API；验证域首页不会重复触发下载授权或消耗接口配额。
      const recoveryNavigation = await navigate(`${new URL(targetUrl).origin}/`);
      result = await verifyPage() ? await fetchOnce() : await navigationResponse(recoveryNavigation);
    }
    if (isCloudflareResponse(result)) {
      verification = {
        ...verification, detected: true, completed: false, manual_required: true,
        kind: "cloudflare", reason: verification?.reason || "challenge_persisted",
      };
    }
    return { ...result, headers: safeResponseHeaders(result.headers), ...(verification ? { verification } : {}) };
  }).finally(() => releaseSession());
}
