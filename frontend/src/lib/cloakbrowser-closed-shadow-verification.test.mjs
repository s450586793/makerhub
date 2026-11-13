import assert from "node:assert/strict";
import { test } from "node:test";
import puppeteer from "puppeteer-core";

import {
  detectVerificationChallenge,
  ensurePageVerification,
  inspectPageVerificationState,
} from "../../../app/services/cloakbrowser_verification.mjs";

const executablePath = process.env.MAKERHUB_TEST_CHROMIUM;

test("closed shadow Turnstile discovery and interaction use real Chromium", {
  skip: !executablePath && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  const browser = await puppeteer.launch({ executablePath, headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--site-per-process"] });
  t.after(() => browser.close());

  async function fixture({ iframeOrigin = "https://challenges.cloudflare.com", width = 300,
    height = 70, hidden = false, complete = true, pageChallenge = true } = {}) {
    const page = await browser.newPage();
    t.after(() => page.close().catch(() => undefined));
    const requests = [];
    const inputs = [];
    const sessions = { attached: 0, detached: 0 };
    let beforeCommand = async () => {};
    const createSession = page.createCDPSession.bind(page);
    page.createCDPSession = async () => {
      const session = await createSession();
      sessions.attached++;
      const send = session.send.bind(session);
      const detach = session.detach.bind(session);
      session.send = async (method, params, options) => {
        if (method === "Input.dispatchMouseEvent") inputs.push(params.type);
        await beforeCommand(method, params);
        return await send(method, params, options);
      };
      session.detach = async () => { sessions.detached++; await detach(); };
      return session;
    };
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = new URL(request.url());
      requests.push(url.pathname);
      if (url.hostname === "makerworld.com") {
        void request.respond({ status: 200, contentType: "text/html", body: `<!doctype html>
          <title>${pageChallenge ? "Just a moment..." : "MakerWorld"}</title>${pageChallenge ? '<div id="challenge-form"></div>' : ''}<div id="widget"></div>
          <script>
            window.fixtureClicks = 0;
            const host = document.querySelector('#widget');
            const shadow = host.attachShadow({ mode: 'closed' });
            const iframe = document.createElement('iframe');
            iframe.src = ${JSON.stringify(`${iframeOrigin}/turnstile/fixture`)};
            iframe.style.cssText = ${JSON.stringify(`position:fixed;left:16px;top:300px;width:${width}px;height:${height}px;border:0;${hidden ? "display:none;" : ""}`)};
            shadow.append(iframe);
            window.fixtureChangeOrigin = () => { iframe.src = 'https://challenges.cloudflare.com.evil.test/replaced'; };
            addEventListener('message', (event) => {
              if (event.origin !== ${JSON.stringify(iframeOrigin)} || event.data !== 'fixture-trusted-click') return;
              window.fixtureClicks++;
              if (${JSON.stringify(complete)}) {
                host.remove();
                document.querySelector('#challenge-form')?.remove();
                document.title = 'MakerWorld';
              }
            });
          </script>` });
      } else if (url.origin === iframeOrigin) {
        void request.respond({ status: 200, contentType: "text/html", body: `<!doctype html>
          <style>html,body{margin:0}</style><div id="checkbox-host"></div><script>
            const shadow = document.querySelector('#checkbox-host').attachShadow({ mode: 'closed' });
            shadow.innerHTML = '<label style="display:flex;align-items:center;width:300px;height:70px;gap:10px"><input type="checkbox" style="margin-left:18px;width:24px;height:24px">Verify you are human</label>';
            shadow.addEventListener('click', (event) => {
              if (event.isTrusted && event.target.matches('input')) parent.postMessage('fixture-trusted-click', '*');
            });
          </script>` });
      } else {
        void request.abort();
      }
    });
    await page.goto("https://makerworld.com/en", { waitUntil: "networkidle0" });
    return { page, requests, inputs, sessions, beforeCommand: (callback) => { beforeCommand = callback; } };
  }

  await t.test("an official iframe inside closed shadow roots is found and receives trusted input", async () => {
    const { page, requests, inputs, sessions } = await fixture();
    assert.equal(await page.$("iframe"), null);
    assert.equal(await page.evaluate(() => document.querySelector('#widget').shadowRoot), null);
    const challenge = await detectVerificationChallenge(page);
    assert.equal(challenge?.provider, "turnstile");
    assert.equal(challenge?.challenge_type, "checkbox");
    const result = await ensurePageVerification(page, { passiveWaitMs: 0, timeoutMs: 3000, maxAttempts: 2 });
    assert.equal(result.completed, true);
    assert.equal(result.verification.attempted, true);
    assert.equal(result.verification.attempts, 1);
    assert.equal(await page.evaluate(() => window.fixtureClicks), 1);
    assert.equal(inputs.filter((type) => type === 'mousePressed').length, 1);
    assert.equal(sessions.detached, sessions.attached);
    assert.equal(requests.some((path) => path.includes('/f3mf') || path.endsWith('.3mf')), false);
  });

  await t.test("a closed shadow widget is recognized even without a whole-page challenge title", async () => {
    const { page, inputs, sessions } = await fixture({ pageChallenge: false });
    assert.deepEqual(await inspectPageVerificationState(page), { detected: true, kind: "cloudflare" });
    const result = await ensurePageVerification(page, { passiveWaitMs: 0, timeoutMs: 3000 });
    assert.equal(result.completed, true);
    assert.equal(inputs.filter((type) => type === 'mousePressed').length, 1);
    assert.equal(sessions.detached, sessions.attached);
  });

  await t.test("failed closed shadow inspections never become a successful no-challenge result", async () => {
    for (const failingMethod of ["attach", "DOM.getDocument", "DOM.resolveNode", "Runtime.callFunctionOn"]) {
      const item = await fixture({ pageChallenge: false });
      if (failingMethod === "attach") {
        item.page.createCDPSession = async () => { throw new Error("CDP fixture unavailable"); };
      } else {
        item.beforeCommand(async (method) => {
          if (method === failingMethod) throw new Error("CDP fixture unavailable");
        });
      }
      const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 2000 });
      assert.equal(result.completed, false, failingMethod);
      assert.equal(result.reason, "discovery_failed", failingMethod);
      assert.equal(result.verification.completed, false, failingMethod);
      assert.equal(item.inputs.length, 0, failingMethod);
      assert.equal(item.sessions.detached, item.sessions.attached, failingMethod);
    }
  });

  await t.test("a failed outcome inspection cannot report that the widget was solved", async () => {
    const item = await fixture({ pageChallenge: false, complete: false });
    item.beforeCommand(async (method) => {
      if (method === "DOM.getDocument" && item.inputs.includes("mouseReleased")) {
        throw new Error("CDP fixture outcome unavailable");
      }
    });
    const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 2500 });
    assert.equal(item.inputs.filter((type) => type === "mousePressed").length, 1);
    assert.equal(result.completed, false);
    assert.equal(result.reason, "discovery_failed");
    assert.equal(result.verification.completed, false);
    assert.equal(item.sessions.detached, item.sessions.attached);
  });

  await t.test("closed shadow discovery rejects lookalike, insecure, nonstandard-port and credential origins", async () => {
    for (const iframeOrigin of ["https://challenges.cloudflare.com.evil.test", "http://challenges.cloudflare.com",
      "https://challenges.cloudflare.com:8443", "https://user@challenges.cloudflare.com"]) {
      const { page, inputs, sessions } = await fixture({ iframeOrigin });
      assert.equal(await detectVerificationChallenge(page), null, iframeOrigin);
      assert.equal(inputs.length, 0);
      assert.equal(sessions.detached, sessions.attached);
    }
  });

  await t.test("hidden or implausibly small closed shadow widgets never receive a press", async () => {
    for (const options of [{ hidden: true }, { width: 30, height: 30 }, { width: 700, height: 70 }]) {
      const { page, inputs, sessions } = await fixture(options);
      const result = await ensurePageVerification(page, { passiveWaitMs: 0, timeoutMs: 600 });
      assert.equal(result.completed, false);
      assert.equal(inputs.includes('mousePressed'), false);
      assert.equal(sessions.detached, sessions.attached);
    }
  });

  await t.test("an iframe removed or redirected after discovery is checked again before the press", async () => {
    for (const action of ["remove", "redirect"]) {
      const item = await fixture({ complete: false });
      let changed = false;
      item.beforeCommand(async (method, params) => {
        if (method !== 'Input.dispatchMouseEvent' || params.type !== 'mouseMoved' || changed) return;
        changed = true;
        await item.page.evaluate((operation) => {
          if (operation === 'remove') document.querySelector('#widget').remove();
          else window.fixtureChangeOrigin();
        }, action);
      });
      const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 2000 });
      assert.equal(result.completed, false);
      assert.equal(changed, true);
      assert.equal(item.inputs.includes('mousePressed'), false);
      assert.equal(item.sessions.detached, item.sessions.attached);
    }
  });

  await t.test("cancellation during the pointer path prevents every subsequent press", async () => {
    const item = await fixture({ complete: false });
    const controller = new AbortController();
    item.beforeCommand(async (method, params) => {
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') controller.abort();
    });
    const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 2000, signal: controller.signal });
    assert.equal(result.completed, false);
    assert.equal(result.reason, 'aborted');
    assert.equal(item.inputs.includes('mousePressed'), false);
    assert.equal(item.sessions.detached, item.sessions.attached);
  });

  await t.test("a changing closed shadow challenge never receives more than two real attempts", async () => {
    const item = await fixture({ complete: false });
    const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 3500, maxAttempts: 20 });
    assert.equal(result.completed, false);
    assert.equal(result.verification.attempts, 2);
    assert.equal(result.verification.reason, 'attempts_exhausted');
    assert.equal(item.inputs.filter((type) => type === 'mousePressed').length, 2);
    assert.equal(await item.page.evaluate(() => window.fixtureClicks), 2);
    assert.equal(item.sessions.detached, item.sessions.attached);
  });

  await t.test("a hanging closed shadow DOM query respects the shared deadline and cleans up", async () => {
    const item = await fixture();
    item.beforeCommand(async (method) => {
      if (method === 'DOM.getDocument') await new Promise(() => {});
    });
    const started = performance.now();
    const result = await ensurePageVerification(item.page, { passiveWaitMs: 0, timeoutMs: 120 });
    assert.equal(result.completed, false);
    assert.equal(result.reason, 'timeout');
    assert.ok(performance.now() - started < 1000);
    assert.equal(item.inputs.length, 0);
    assert.equal(item.sessions.detached, item.sessions.attached);
  });
});
