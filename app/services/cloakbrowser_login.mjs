import { ensurePageVerification, inspectPageVerificationState } from "./cloakbrowser_verification.mjs";

const PLATFORM_LOGIN_CONFIG = {
  cn: {
    bambuOrigin: "https://bambulab.cn",
    makerWorldOrigin: "https://makerworld.com.cn",
  },
  global: {
    bambuOrigin: "https://bambulab.com",
    makerWorldOrigin: "https://makerworld.com",
  },
};

export function normalizePlatform(platform) {
  return String(platform || "").trim().toLowerCase() === "global" ? "global" : "cn";
}

function hostnameMatchesCookieDomain(hostname, domain) {
  const cleanHostname = String(hostname || "").trim().toLowerCase();
  const cleanDomain = String(domain || "").trim().toLowerCase().replace(/^\.+/, "");
  return Boolean(cleanHostname && cleanDomain) && (
    cleanHostname === cleanDomain || cleanHostname.endsWith(`.${cleanDomain}`)
  );
}

function isAuthTokenCookie(cookie) {
  return ["token", "access_token", "accesstoken"].includes(
    String(cookie?.name || "").trim().toLowerCase(),
  );
}

export function browserAuthTokenForUrl(cookies, targetUrl) {
  let hostname = "";
  try {
    hostname = new URL(String(targetUrl || "")).hostname;
  } catch {
    return "";
  }
  const match = (Array.isArray(cookies) ? cookies : []).find((cookie) => (
    isAuthTokenCookie(cookie)
    && String(cookie?.value || "").trim()
    && hostnameMatchesCookieDomain(hostname, cookie?.domain)
  ));
  return String(match?.value || "").trim();
}

export function hasMakerWorldSessionCookie(cookies, platform, pathname = "/") {
  const makerWorldHost = new URL(
    PLATFORM_LOGIN_CONFIG[normalizePlatform(platform)].makerWorldOrigin,
  ).hostname;
  return (Array.isArray(cookies) ? cookies : []).some((cookie) => {
    const path = cookie?.path === undefined ? "/" : cookie.path;
    return isAuthTokenCookie(cookie)
      && String(cookie?.value || "").trim()
      && String(cookie?.domain || "").trim().toLowerCase().replace(/^\.+/, "") === makerWorldHost
      && (cookie.expires === undefined || cookie.expires === -1 || (
        typeof cookie.expires === "number" && Number.isFinite(cookie.expires) && cookie.expires > Date.now() / 1000
      ))
      && typeof path === "string" && path.startsWith("/")
      && (pathname === path || pathname.startsWith(path.endsWith("/") ? path : `${path}/`));
  });
}

export function makerWorldHomeUrl(platform) {
  return `${PLATFORM_LOGIN_CONFIG[normalizePlatform(platform)].makerWorldOrigin}/zh`;
}

export function isMakerWorldUrl(value, platform) {
  const config = PLATFORM_LOGIN_CONFIG[normalizePlatform(platform)];
  try {
    const parsed = new URL(String(value || ""));
    return parsed.origin === config.makerWorldOrigin && !parsed.username && !parsed.password;
  } catch {
    return false;
  }
}

export function isMakerWorldTicketUrl(value, platform) {
  try {
    const parsed = new URL(String(value || ""));
    const destination = new URL(parsed.searchParams.get("to") || "");
    return isMakerWorldUrl(parsed.href, platform)
      && parsed.pathname.replace(/\/$/, "") === "/api/sign-in/ticket"
      && Boolean(String(parsed.searchParams.get("ticket") || "").trim())
      && isMakerWorldUrl(destination.href, platform)
      && destination.pathname.replace(/\/$/, "") !== "/api/sign-in/ticket";
  } catch {
    return false;
  }
}

export function makerWorldLoginUrl(platform) {
  const config = PLATFORM_LOGIN_CONFIG[normalizePlatform(platform)];
  const callback = `${config.makerWorldOrigin}/api/sign-in/ticket?to=${config.makerWorldOrigin}/zh`;
  return `${config.bambuOrigin}/zh-cn/sign-in?ticket=1&to=${encodeURIComponent(callback)}`;
}

export async function readMakerWorldAuthenticationState(context, page, platform) {
  const failed = (authentication_reason) => ({ authenticated: false, authentication_reason });
  try {
    const challenge = await inspectPageVerificationState(page);
    if (challenge.detected) return failed("verification_required");
    const current = new URL(page.url());
    if (!isMakerWorldUrl(current.href, platform)
      || /^\/api\/sign-in\/ticket\/?$/i.test(current.pathname)) return failed("auth_required");
    const state = await page.evaluate(() => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0"
          && box.width > 0 && box.height > 0;
      };
      return {
        loaded: Boolean(document.body) && document.readyState === "complete",
        login_visible: [...document.querySelectorAll("button, a, [role='button']")].some((element) => (
          visible(element) && /^(?:登录|登入|登錄|log\s*in|sign\s*in)$/i.test(
            String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim(),
          )
        )),
      };
    });
    const cookies = await context.cookies();
    if (!state.loaded || state.login_visible || !hasMakerWorldSessionCookie(cookies, platform, current.pathname)) {
      return failed("auth_required");
    }
    return { authenticated: true, authentication_reason: "ready" };
  } catch {
    return failed("navigation_failed");
  }
}

export async function completeMakerWorldTicketSession(context, page, platform, options = {}) {
  const failed = (authentication_reason, login_fallback_attempted = false) => ({
    authenticated: false, authentication_reason, login_fallback_attempted,
  });
  if (!isMakerWorldTicketUrl(options.targetUrl, platform)) return failed("navigation_failed");
  const timeoutMs = Math.max(1, Math.min(Number(options.timeoutMs) || 50000, 50000));
  const deadline = Math.min(Number(options.deadlineMs) || Infinity, Date.now() + timeoutMs);
  const controller = new AbortController();
  let attemptsRemaining = Math.max(1, Math.min(Number(options.verificationMaxAttempts) || 2, 2));
  const remaining = () => Math.max(0, deadline - Date.now());
  const bounded = async (operation) => {
    if (!remaining()) throw new Error("login deadline exceeded");
    let timer;
    try {
      return await Promise.race([operation(), new Promise((_resolve, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("login deadline exceeded"));
        }, remaining());
      })]);
    } finally {
      clearTimeout(timer);
    }
  };
  const navigate = (url) => bounded(() => page.goto(url, {
    waitUntil: "domcontentloaded",
    timeout: Math.max(1, Math.min(Number(options.navigationTimeoutMs) || 30000, remaining())),
  }));
  const settle = (milliseconds) => bounded(() => new Promise((resolve) => {
    setTimeout(resolve, Math.min(milliseconds, remaining()));
  }));
  const verifyPage = async () => {
    const challenge = await bounded(() => inspectPageVerificationState(page));
    if (!challenge.detected) return true;
    if (options.autoVerifyChallenges !== true || attemptsRemaining <= 0) return false;
    const verify = options.ensurePageVerification || ensurePageVerification;
    const result = await bounded(() => verify(page, {
      allowInteraction: true,
      timeoutMs: Math.max(1, Math.min(Number(options.verificationTimeoutMs) || 50000, remaining())),
      passiveWaitMs: Math.min(5000, remaining()),
      maxAttempts: attemptsRemaining,
      signal: controller.signal,
    }));
    const observed = Number(result.verification?.attempts);
    const used = result.verification?.attempted === true ? Math.max(1, Number.isFinite(observed) ? observed : 1) : 0;
    attemptsRemaining -= Math.min(attemptsRemaining, used);
    return result.completed === true;
  };
  let fallback = false;
  try {
    try {
      await navigate(options.targetUrl);
    } catch (error) {
      if (error?.name !== "TimeoutError") throw error;
    }
    // ticket 回调可能在 hydration 时清空旧 token，保留原 seed 的被动稳定等待。
    await settle(1000);
    if (!await verifyPage()) return failed("verification_required");
    let state = await bounded(() => readMakerWorldAuthenticationState(context, page, platform));
    if (state.authenticated || state.authentication_reason === "verification_required") {
      return { ...state, login_fallback_attempted: false };
    }
    if (!isMakerWorldUrl(page.url(), platform) && !isBambuLoginConfirmationUrl(page.url(), platform)) {
      return failed("navigation_failed");
    }
    fallback = true;
    await navigate(makerWorldLoginUrl(platform));
    if (!await verifyPage()) return failed("verification_required", true);
    state = await bounded(() => readMakerWorldAuthenticationState(context, page, platform));
    if (state.authentication_reason === "verification_required") return { ...state, login_fallback_attempted: true };
    if (!state.authenticated && isBambuLoginConfirmationUrl(page.url(), platform) && remaining() >= 1000) {
      await bounded(() => completeBambuLoginConfirmation(context, page, platform, remaining(), {
        signal: controller.signal,
      }));
    }
    await settle(250);
    if (!await verifyPage()) return failed("verification_required", true);
    state = await bounded(() => readMakerWorldAuthenticationState(context, page, platform));
    return { ...state, login_fallback_attempted: true };
  } catch {
    return failed("navigation_failed", fallback);
  } finally {
    controller.abort();
  }
}

export function isBambuLoginConfirmationUrl(value, platform) {
  const config = PLATFORM_LOGIN_CONFIG[normalizePlatform(platform)];
  try {
    const parsed = new URL(String(value || ""));
    const target = new URL(parsed.searchParams.get("to") || "");
    return parsed.origin === config.bambuOrigin
      && !parsed.username && !parsed.password
      && /\/sign-in\/?$/i.test(parsed.pathname)
      && parsed.searchParams.get("ticket") === "1"
      && target.origin === config.makerWorldOrigin
      && !target.username && !target.password
      && target.pathname.replace(/\/$/, "") === "/api/sign-in/ticket";
  } catch {
    return false;
  }
}

export async function completeBambuLoginConfirmation(context, page, platform, timeoutMs = 30000, options = {}) {
  if (!isBambuLoginConfirmationUrl(page.url(), platform)) return false;
  const timeout = Math.max(Math.min(Number(timeoutMs || 30000), 120000), 1000);
  const deadline = Date.now() + timeout;
  const actionDeadline = Math.min(deadline, Date.now() + 10000);
  let clicked = false;
  while (Date.now() < actionDeadline && !options.signal?.aborted) {
    let actionState = "loading";
    try {
      actionState = await page.evaluate(({ deadline: interactionDeadline }) => {
        if (Date.now() >= interactionDeadline) return "expired";
        const visibleActions = [...document.querySelectorAll("button, a, [role='button']")].filter((element) => {
          const style = window.getComputedStyle(element);
          const bounds = element.getBoundingClientRect();
          return style.display !== "none"
            && style.visibility !== "hidden"
            && bounds.width > 0
            && bounds.height > 0
            && !element.hasAttribute("disabled");
        });
        const actionText = (element) => String(element.innerText || element.textContent || "")
          .replace(/\s+/g, " ")
          .trim();
        const signedIn = visibleActions.some((element) => (
          /^(?:登出|退出登录|sign out|log out)$/i.test(actionText(element))
        ));
        const continueAction = visibleActions.find((element) => (
          /^(?:继续|continue)$/i.test(actionText(element))
        ));
        if (signedIn && continueAction) {
          if (Date.now() >= interactionDeadline) return "expired";
          continueAction.click();
          return "clicked";
        }
        const loginInput = document.querySelector(
          "input:not([type]), input[type='text'], input[type='password'], input[type='email'], input[type='tel']",
        );
        if (loginInput) {
          return "signed_out";
        }
        return "loading";
      }, { deadline: actionDeadline });
    } catch {
      actionState = "loading";
    }
    if (actionState === "clicked" || actionState === true) {
      clicked = true;
      break;
    }
    if (actionState === "signed_out" || actionState === "expired") return false;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!clicked) return false;

  while (Date.now() < deadline && !options.signal?.aborted) {
    try {
      const cookies = await context.cookies();
      const current = new URL(page.url());
      if (isMakerWorldUrl(current.href, platform) && current.pathname.replace(/\/$/, "") !== "/api/sign-in/ticket"
        && hasMakerWorldSessionCookie(cookies, platform, current.pathname)) {
        return true;
      }
    } catch {
      // The page can briefly detach while the SSO redirect replaces the document.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}
