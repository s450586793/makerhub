import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";


const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(__dirname, "..", "..");
const requireFromFrontend = createRequire(path.join(ROOT_DIR, "frontend", "node_modules", "package.json"));
const puppeteer = requireFromFrontend("puppeteer-core");

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

function isMakerWorldPage(page, platform) {
  try {
    const hostname = new URL(page.url()).hostname.toLowerCase();
    const domain = platform === "global" ? "makerworld.com" : "makerworld.com.cn";
    return hostname === domain || hostname.endsWith(`.${domain}`);
  } catch {
    return false;
  }
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

async function pageForAuthorization(context, platform, navigationTimeoutMs) {
  const pages = await context.pages();
  const existing = pages.find((page) => isMakerWorldPage(page, platform));
  if (existing) return existing;
  const page = pages[0] || await context.newPage();
  const domain = platform === "global" ? "makerworld.com" : "makerworld.com.cn";
  await page.goto(`https://${domain}/zh`, {
    waitUntil: "domcontentloaded",
    timeout: Math.max(Number(navigationTimeoutMs || 30000), 15000),
  });
  return page;
}

async function browserAuthorizationToken(page) {
  try {
    return await page.evaluate(() => {
      const tokenNames = new Set(["token", "accesstoken", "access_token"]);
      for (const storage of [window.localStorage, window.sessionStorage]) {
        for (let index = 0; index < storage.length; index += 1) {
          const key = storage.key(index);
          if (!key || !tokenNames.has(key.toLowerCase())) continue;
          const value = storage.getItem(key);
          if (typeof value === "string" && value) return value;
        }
      }
      return "";
    });
  } catch {
    return "";
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

async function fetchAuthorization(page, targetUrl) {
  if (!isThreeMfAuthorizationUrl(targetUrl)) {
    throw new Error("invalid 3MF authorization URL");
  }
  const authToken = await browserAuthorizationToken(page);
  const response = await page.evaluate(async ({ url, token }) => {
    const headers = { Accept: "application/json, text/plain, */*" };
    if (token) {
      headers.Authorization = `Bearer ${token}`;
      headers.token = token;
      headers["X-Token"] = token;
      headers["X-Access-Token"] = token;
    }
    try {
      const response = await fetch(url, {
        credentials: "include",
        headers,
        cache: "no-store",
      });
      return {
        status_code: response.status,
        text: (await response.text()).slice(0, 16384),
      };
    } catch {
      return {
        status_code: 0,
        text: "",
      };
    }
  }, { url: targetUrl, token: authToken });
  let payload = null;
  try {
    payload = response.text ? JSON.parse(response.text) : null;
  } catch {
    payload = null;
  }
  return {
    status_code: Number(response.status_code || 0),
    payload: sanitizedAuthorizationPayload(payload, response.text),
    text: payload ? "" : String(response.text || "").slice(0, 1024),
  };
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
    protocolTimeout: Math.max(Number(input.navigation_timeout_ms || 30000), 15000),
  });
  let navigationError = "";
  try {
    const contexts = browser.browserContexts();
    const context = contexts[0] || browser.defaultBrowserContext();
    if (input.action === "fetch") {
      const page = await pageForAuthorization(context, String(input.platform || "cn"), input.navigation_timeout_ms);
      const authorization = await fetchAuthorization(page, String(input.target_url || ""));
      process.stdout.write(JSON.stringify({ ok: true, ...authorization }));
      return;
    }
    if (input.action === "seed") {
      const cookies = (Array.isArray(input.cookies) ? input.cookies : []).map(cleanCookie).filter(Boolean);
      if (cookies.length) await context.setCookie(...cookies);
    }
    const pages = await context.pages();
    const page = pages[0] || await context.newPage();
    const currentUrl = page.url();
    const shouldNavigate = input.target_url && (
      input.action === "seed" || !/^https?:\/\//i.test(currentUrl)
    );
    if (shouldNavigate) {
      try {
        await page.goto(String(input.target_url), {
          waitUntil: "domcontentloaded",
          timeout: Math.max(Number(input.navigation_timeout_ms || 30000), 15000),
        });
        await new Promise((resolve) => setTimeout(resolve, 1000));
      } catch (error) {
        navigationError = error instanceof Error ? error.message : String(error || "navigation failed");
      }
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

main().catch((error) => {
  process.stderr.write((error instanceof Error ? error.message : String(error || "CDP bridge failed")) + "\n");
  process.exit(1);
});
