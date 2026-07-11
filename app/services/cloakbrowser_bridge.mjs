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
  return token ? { Authorization: `Bearer ${token}` } : {};
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
    if (input.action === "seed") {
      const cookies = (Array.isArray(input.cookies) ? input.cookies : []).map(cleanCookie).filter(Boolean);
      if (cookies.length) await context.setCookie(...cookies);
    }
    const pages = await context.pages();
    const page = pages[0] || await context.newPage();
    if (input.target_url) {
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
