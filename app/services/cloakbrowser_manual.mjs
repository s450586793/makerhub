import { createRequire } from "node:module";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { findThreeMfDownloadButton, readAuthorizationResponse } from "./cloakbrowser_bridge.mjs";
import { assertThreeMfTarget, readThreeMfModelState } from "./cloakbrowser_preflight.mjs";
import { hasMakerWorldSessionCookie } from "./cloakbrowser_login.mjs";
import { inspectPageVerificationState } from "./cloakbrowser_verification.mjs";

const ROOT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const requireFrontend = createRequire(path.join(ROOT_DIR, "frontend", "node_modules", "package.json"));
const WIDTH = 1080;
const HEIGHT = 780;
const KEYS = new Set(["Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Space"]);
const MODIFIERS = { Ctrl: "Control", Control: "Control", Meta: "Meta", Alt: "Alt", Shift: "Shift" };

function platformDomains(platform) {
  if (!["cn", "global"].includes(platform)) throw new Error("invalid platform");
  return platform === "cn" ? ["makerworld.com.cn", "bambulab.cn"] : ["makerworld.com", "bambulab.com"];
}

export function isManualPageUrl(value, platform) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443")
      && platformDomains(platform).some(domain => url.hostname === domain || url.hostname.endsWith(`.${domain}`));
  } catch { return false; }
}

function keyParts(key) {
  if (typeof key !== "string" || key.length > 50) throw new Error("invalid key");
  const parts = key.split("+");
  const last = parts.pop();
  if (!KEYS.has(last) && !/^[a-z0-9]$/i.test(last)) throw new Error("invalid key");
  if (parts.length > 3 || parts.some(part => !MODIFIERS[part])) throw new Error("invalid modifier");
  return { modifiers: [...new Set(parts.map(part => MODIFIERS[part]))], key: last === "Space" ? " " : /^[a-z]$/i.test(last) ? last.toLowerCase() : last };
}

export function normalizeManualEvents(events, width = WIDTH, height = HEIGHT) {
  if (!Array.isArray(events) || events.length > 80) throw new Error("invalid input batch");
  return events.map(event => {
    if (!event || typeof event !== "object") throw new Error("invalid event");
    const { type } = event;
    if (["pointerdown", "pointermove", "pointerup"].includes(type)) {
      const { x, y } = event;
      if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x >= width || y >= height) throw new Error("invalid coordinates");
      const delay_ms = event.delay_ms ?? 0;
      if (!Number.isFinite(delay_ms) || delay_ms < 0 || delay_ms > 50) throw new Error("invalid input timing");
      return { type, x, y, delay_ms };
    }
    if (type === "wheel" && Number.isFinite(event.delta_y) && Math.abs(event.delta_y) <= 2000) {
      if (event.x === undefined && event.y === undefined) return { type, delta_y: event.delta_y };
      if (!Number.isFinite(event.x) || !Number.isFinite(event.y) || event.x < 0 || event.y < 0 || event.x >= width || event.y >= height) throw new Error("invalid wheel coordinates");
      return { type, x: event.x, y: event.y, delta_y: event.delta_y };
    }
    if (type === "key") return { type, ...keyParts(event.key) };
    if (type === "text" && typeof event.text === "string" && event.text.length <= 2000 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(event.text)) return { type, text: event.text };
    throw new Error("invalid input event");
  });
}

export async function dispatchManualEvents(page, events, width = WIDTH, height = HEIGHT) {
  for (const event of normalizeManualEvents(events, width, height)) {
    if (event.type.startsWith("pointer")) {
      if (event.delay_ms) await new Promise(resolve => setTimeout(resolve, event.delay_ms));
      await page.mouse.move(event.x, event.y);
      if (event.type === "pointerdown") await page.mouse.down({ button: "left" });
      if (event.type === "pointerup") await page.mouse.up({ button: "left" });
    } else if (event.type === "wheel") {
      if (event.x !== undefined) await page.mouse.move(event.x, event.y);
      await page.mouse.wheel({ deltaY: event.delta_y });
    }
    else if (event.type === "text") await page.keyboard.insertText(event.text);
    else {
      try {
        for (const modifier of event.modifiers) await page.keyboard.down(modifier);
        await page.keyboard.press(event.key);
      } finally {
        for (const modifier of [...event.modifiers].reverse()) await page.keyboard.up(modifier);
      }
    }
  }
}

export function createManualRequestPolicy(platform, instanceId) {
  const domains = platformDomains(platform);
  let granted = false;
  let dispatched = false;
  let challenge = false;
  let finished = false;
  let continuations = 0;
  const authorizationHosts = [domains[0], `api.${domains[1]}`];
  return {
    authorize() { if (granted) throw new Error("authorization already started"); granted = true; },
    observe(response) {
      if (response.status_code >= 200 && response.status_code < 300 && response.payload?.url) finished = true;
      challenge = !finished && (response.status_code === 418 || Boolean(response.payload?.captchaId));
    },
    allow(request) {
      let url;
      try { url = new URL(request.url); } catch { return false; }
      if (request.mainNavigation && !isManualPageUrl(url.href, platform)) return false;
      if (/\.(?:3mf|zip|stl)(?:\/)?$/i.test(url.pathname)) return false;
      const match = url.pathname.match(/^\/(?:api\/)?v1\/design-service\/instance\/([0-9]+)\/f3mf\/?$/i);
      if (!match) return true;
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")
        || !authorizationHosts.includes(url.hostname) || match[1] !== instanceId || finished) return false;
      if (request.method === "OPTIONS") return granted;
      if (!["GET", "POST"].includes(request.method) || !granted) return false;
      if (!dispatched) { dispatched = true; return true; }
      const headers = request.headers || {};
      const captchaHeader = Object.entries(headers).some(([key, value]) => /captcha/i.test(key) && String(value).trim());
      const captchaParam = [...url.searchParams.entries()].some(([key, value]) => /captcha/i.test(key) && value);
      const captchaBody = /"(?:captcha[^"\s]*|validate|seccode)"\s*:\s*"[^"\s]+"/i.test(request.postData || "");
      if (!challenge || !(captchaHeader || captchaParam || captchaBody) || continuations >= 6) return false;
      continuations += 1;
      return true;
    },
  };
}

function restricted(model) {
  return model?.threeMfDownloadAllowed === false || model?.downloadState === "not_downloadable"
    || model?.threeMfSkipReason === "crowdfunding" || Boolean(model?.crowdfundingInfo?.projectId)
    || [true, 1, "1", "true"].includes(model?.paidSetting?.crowdfunding)
    || [model?.license, model?.licenseName, model?.licenseDescriptionInfo].some(value => /sdfl-ppo|platform print only/i.test(String(value || "")));
}

export async function createManualVerification(browser, input) {
  const platform = String(input.platform || "");
  const modelUrl = String(input.model_url || "");
  const instanceId = String(input.instance_id || "");
  const initial = new URL(modelUrl);
  if (!isManualPageUrl(modelUrl, platform) || initial.hostname !== platformDomains(platform)[0]
    || (instanceId && (!/^[1-9][0-9]*$/.test(instanceId) || !/\/models\/[1-9][0-9]*/i.test(initial.pathname)))) throw new Error("invalid verification target");
  const lifetime = Math.max(1000, Math.min(Number(input.lifetime_ms) || 600000, 600000));
  const deadline = Date.now() + lifetime;
  const context = browser.browserContexts()[0] || browser.defaultBrowserContext();
  const page = await context.newPage();
  let closed = false;
  let closing;
  let cdp;
  let loaded = false;
  let status = "loading";
  let message = "正在打开验证页面。";
  let authorization = null;
  let preflight = null;
  let operation = null;
  const policy = createManualRequestPolicy(platform, instanceId);
  const pending = new Set();
  const track = promise => { pending.add(promise); void promise.finally(() => pending.delete(promise)).catch(() => {}); };
  const onRequest = request => {
    const task = (async () => {
      const allowed = policy.allow({ url: request.url(), method: request.method(), headers: request.headers(),
        postData: request.postData(), mainNavigation: request.isNavigationRequest() && request.frame() === page.mainFrame() });
      await (allowed ? request.continue() : request.abort("blockedbyclient"));
    })().catch(() => {});
    track(task);
  };
  const onResponse = response => {
    const task = (async () => {
      const url = new URL(response.url());
      if (!url.pathname.match(new RegExp(`/instance/${instanceId}/f3mf/?$`, "i"))
        || !["GET", "POST"].includes(response.request().method()) || !isManualPageUrl(url.href, platform)
        || !["authorizing", "challenge"].includes(status)) return;
      const result = await readAuthorizationResponse(response);
      policy.observe(result);
      if (result.status_code >= 200 && result.status_code < 300 && /^https:\/\//i.test(result.payload?.url || "")) {
        authorization = result;
        status = "authorized";
        message = "验证已通过，正在确认账号状态。";
      } else if (result.status_code === 418 || result.payload?.captchaId) {
        authorization = result;
        status = "challenge";
        message = "请在画面中完成验证码。";
      } else {
        authorization = result;
        status = "error";
        message = result.status_code === 429 ? "源站限制了下载，请稍后重试。" : "下载授权未通过，请检查账号状态。";
      }
    })().catch(() => { status = "error"; message = "无法读取验证结果，请重新打开验证。"; });
    track(task);
  };
  const close = () => closing ||= (async () => {
    closed = true;
    page.off("response", onResponse);
    await page.close().catch(() => {});
    await Promise.allSettled([...pending]);
    await cdp?.detach().catch(() => {});
    return { status: "closed" };
  })();
  const assertPage = () => {
    if (closed || page.isClosed()) throw new Error("verification page closed");
    if (Date.now() >= deadline) throw new Error("verification session expired");
    if (!isManualPageUrl(page.url(), platform)) throw new Error("verification page outside selected platform");
  };
  try {
    await page.setViewport({ width: WIDTH, height: HEIGHT, deviceScaleFactor: 1 });
    await page.setRequestInterception(true);
    page.on("request", onRequest);
    page.on("response", onResponse);
    page.on("popup", popup => { void popup.close().catch(() => {}); });
    await page.evaluateOnNewDocument(() => {
      // 人工页只使用一个窗口，防止新窗口绕过本页的授权预约与下载拦截。
      Object.defineProperty(window, "open", { value: () => null, writable: false, configurable: false });
      document.addEventListener("click", event => {
        const link = event.target.closest?.("a[target]");
        if (link && !["", "_self", "_top", "_parent"].includes(link.target.toLowerCase())) event.preventDefault();
      }, true);
      document.addEventListener("submit", event => {
        const target = String(event.target.target || "").toLowerCase();
        if (target && !["_self", "_top", "_parent"].includes(target)) event.preventDefault();
      }, true);
    });
    cdp = await page.createCDPSession();
    await cdp.send("Page.setDownloadBehavior", { behavior: "deny" });
    // 再关闭仍由浏览器创建的子窗口，页面脚本在加载前已禁止新窗口入口。
    cdp.on("Target.attachedToTarget", event => {
      if (event.targetInfo.type === "page") track(cdp.send("Target.closeTarget", { targetId: event.targetInfo.targetId }).catch(() => {}));
    });
    await cdp.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true,
      filter: [{ type: "page", exclude: false }, { exclude: true }] });
    operation = (async () => {
      try {
        await page.goto(instanceId ? `${initial.origin}${initial.pathname}#profileId-${instanceId}` : modelUrl,
          { waitUntil: "domcontentloaded", timeout: 30000 });
      } catch (error) {
        if (error?.name !== "TimeoutError" && !closed) { status = "error"; message = "验证页面加载失败，请稍后重试。"; }
      } finally { loaded = true; }
    })();
  } catch (error) { await close(); throw error; }

  const frame = async ({ snapshot = false } = {}) => {
    if (!loaded && page.url() === "about:blank") return { status, message, width: WIDTH, height: HEIGHT };
    assertPage();
    if (loaded && !["authorizing", "challenge", "authorized", "error"].includes(status)) {
      const verification = await inspectPageVerificationState(page);
      if (verification.detected) { status = "page_verification"; message = "请在画面中完成页面验证。"; }
      else if (instanceId) {
        const state = await readThreeMfModelState(page, platform, modelUrl);
        const authenticated = hasMakerWorldSessionCookie(await context.cookies(), platform) && !state.login_visible;
        const instance = state.model?.instances.find(item => item.id === instanceId);
        if (!authenticated) { status = "auth_required"; message = "请在画面中登录当前账号。"; }
        else if (restricted(state.model) || restricted(instance)) { status = "error"; message = "该模型不支持归档 3MF，请选择其他模型。"; }
        else if (state.reason === "ready" && instance) {
          try {
            await assertThreeMfTarget(page, { platform, modelUrl, instanceId, timeoutMs: 0 });
            preflight = { ready: true, reason: "ready", authenticated: true, model: state.model };
            status = "ready"; message = "正在准备下载验证。";
          } catch { status = "page_verification"; message = "请在画面中选择待验证的打印配置。"; }
        } else { status = "page_verification"; message = "正在等待模型页面加载。"; }
      } else { status = "page_ready"; message = "页面已打开，验证完成后点击完成。"; }
    }
    const result = { status, message, width: WIDTH, height: HEIGHT,
      image: await page.screenshot({ type: "jpeg", quality: 75, encoding: "base64" }),
      ...(preflight ? { preflight } : {}), ...(authorization ? { authorization } : {}) };
    if (snapshot || status === "authorized") {
      const cookies = await context.cookies();
      const storage = await page.evaluate(() => {
        const select = store => Object.fromEntries(Object.keys(store).filter(key => /token|auth|session/i.test(key))
          .filter(key => String(store.getItem(key) || "").length <= 16384).map(key => [key, store.getItem(key)]));
        return { origin: location.origin, local: select(localStorage), session: select(sessionStorage) };
      }).catch(() => null);
      result.snapshot = { cookies, storage: storage ? [storage] : [], current_url: page.url(),
        authenticated: hasMakerWorldSessionCookie(cookies, platform) };
    }
    return result;
  };
  return {
    async command(command) {
      if (command.action === "close") return close();
      if (command.action === "frame") return frame(command);
      assertPage();
      if (command.action === "input") {
        await dispatchManualEvents(page, command.events);
        return frame();
      }
      if (command.action === "authorize") {
        if (status !== "ready" || !preflight?.ready) throw new Error("verification target not ready");
        status = "authorizing"; message = "正在打开下载验证码。";
        operation = (async () => {
          let button;
          try {
            await assertThreeMfTarget(page, { platform, modelUrl, instanceId });
            button = await findThreeMfDownloadButton(page, 15000);
            assertPage();
            await assertThreeMfTarget(page, { platform, modelUrl, instanceId, timeoutMs: 0 });
            policy.authorize();
            await button.click({ delay: 20 });
          } catch {
            if (!closed) { status = "error"; message = "无法打开下载验证，请重新尝试。"; }
          } finally { await button?.dispose().catch(() => {}); }
        })();
        return { status, message, width: WIDTH, height: HEIGHT };
      }
      throw new Error("invalid command");
    },
    close,
  };
}

async function main() {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let browser;
  let session;
  let timer;
  let initializing;
  let stopping = false;
  const shutdown = () => {
    stopping = true;
    lines.close();
    process.stdin.destroy();
    void (async () => {
      await initializing?.catch(() => {});
      await session?.close();
      await browser?.disconnect();
      process.exit(0);
    })();
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  try {
    for await (const line of lines) {
      let result;
      try {
        if (Buffer.byteLength(line) > 100000) throw new Error("command too large");
        const input = JSON.parse(line);
        if (input.action === "init") {
          if (session) throw new Error("session already initialized");
          initializing = (async () => {
            const headers = { Authorization: `Bearer ${input.auth_token}` };
            const version = await fetch(`${input.cdp_url.replace(/\/$/, "")}/json/version`, { headers, signal: AbortSignal.timeout(15000) });
            if (!version.ok) throw new Error("CDP unavailable");
            const endpoint = (await version.json()).webSocketDebuggerUrl;
            const puppeteer = requireFrontend("puppeteer-core");
            browser = await puppeteer.connect({ browserWSEndpoint: endpoint, headers, defaultViewport: null, protocolTimeout: 20000 });
            session = await createManualVerification(browser, input);
          })();
          await initializing;
          if (stopping) break;
          timer = setTimeout(shutdown, Math.min(Number(input.lifetime_ms) || 600000, 600000));
          result = { status: "loading", message: "正在打开验证页面。", width: WIDTH, height: HEIGHT };
        } else {
          if (!session) throw new Error("session missing");
          result = await session.command(input);
        }
        process.stdout.write(JSON.stringify(result) + "\n");
        if (input.action === "close") break;
      } catch {
        process.stdout.write(JSON.stringify({ status: "error", message: "验证会话操作失败，请关闭后重新验证。" }) + "\n");
      }
    }
  } finally {
    clearTimeout(timer);
    await session?.close();
    await browser?.disconnect();
    process.off("SIGTERM", shutdown);
    process.off("SIGINT", shutdown);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { process.stderr.write("Manual verification bridge failed.\n"); process.exitCode = 1; });
}
