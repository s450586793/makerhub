import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import puppeteer from "puppeteer-core";

import {
  cleanupStaleAutomationTargets,
  coordinateThreeMfAuthorization,
  withTemporaryTarget,
} from "../../../app/services/cloakbrowser_bridge.mjs";
import { installThreeMfAuthorizationGuard } from "../../../app/services/cloakbrowser_preflight.mjs";

const executablePath = process.env.MAKERHUB_TEST_CHROMIUM;

test("CORS preflight cannot consume the page-generated 3MF authorization response", {
  skip: !executablePath && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "makerhub-cors-authorization-"));
  let browser;
  let server;
  try {
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
      "-subj", "/CN=makerworld.com.cn", "-keyout", path.join(directory, "key.pem"),
      "-out", path.join(directory, "cert.pem")], { stdio: "ignore" });
    const authorizationMethods = [];
    let sdkRequests = 0;
    const html = `<!doctype html><meta charset="utf-8"><title>Local CORS fixture</title>
      <button class="primaryButton" onclick="window.fixtureDownload(event)">Download 3MF</button>
      <script>
        window.fixtureDownload = async (event) => {
          window.fixtureClicks = (window.fixtureClicks || 0) + 1;
          window.fixtureTrustedClick = event.isTrusted;
          const response = await fetch('https://api.bambulab.cn/v1/design-service/instance/123/f3mf');
          window.fixtureReceived418 = response.status === 418;
          const payload = await response.json();
          window.fixtureReceivedCaptcha = typeof payload.captchaId === 'string';
          if (window.fixtureReceived418 && window.fixtureReceivedCaptcha) {
            const sdk = document.createElement('script');
            sdk.src = '/fixture-captcha-sdk.js';
            document.head.append(sdk);
          }
        };
      </script>`;
    server = https.createServer({
      key: await readFile(path.join(directory, "key.pem")),
      cert: await readFile(path.join(directory, "cert.pem")),
    }, (request, response) => {
      if (request.url === "/v1/design-service/instance/123/f3mf") {
        authorizationMethods.push(request.method);
        const headers = {
          "Content-Type": "application/json",
          "Access-Control-Allow-Origin": "https://makerworld.com.cn",
          "Access-Control-Allow-Headers": "x-bbl-captcha-result",
          "Access-Control-Allow-Methods": "GET",
        };
        if (request.method === "OPTIONS") {
          response.writeHead(200, headers);
          response.end();
          return;
        }
        const verified = request.headers["x-bbl-captcha-result"] === "local-fixture";
        response.writeHead(verified ? 200 : 418, headers);
        response.end(JSON.stringify(verified
          ? { name: "fixture.3mf", url: "https://download.example.test/fixture.3mf" }
          : { captchaId: "local-fixture" }));
      } else if (request.url === "/fixture-captcha-sdk.js") {
        sdkRequests++;
        response.writeHead(200, { "Content-Type": "application/javascript" });
        // 模拟 bind 静默验证后的网页重试，不挂载验证码 DOM，也不请求文件。
        response.end(`window.fixtureSdkLoaded = true;
          fetch('https://api.bambulab.cn/v1/design-service/instance/123/f3mf',
            {headers:{'x-bbl-captcha-result':'local-fixture'}})
            .then(response => response.json())
            .then(payload => window.fixtureSignedUrlReceived = !!payload.url);`);
      } else if (request.url === "/zh/models/456") {
        response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        response.end(html);
      } else {
        response.writeHead(404);
        response.end();
      }
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const port = server.address().port;
    browser = await puppeteer.launch({ executablePath, headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking",
        "--no-proxy-server", "--ignore-certificate-errors",
        `--host-resolver-rules=MAP makerworld.com.cn:443 127.0.0.1:${port},MAP api.bambulab.cn:443 127.0.0.1:${port}`],
    });
    const context = browser.defaultBrowserContext();
    await cleanupStaleAutomationTargets(browser, context, path.join(directory, "targets.json"));
    await withTemporaryTarget(browser, context, { hidden: false }, async target => {
      const page = await target.page();
      const guard = await installThreeMfAuthorizationGuard(page, { platform: "cn", instanceId: "123" });
      try {
        await page.goto("https://makerworld.com.cn/zh/models/456#profileId-123", { waitUntil: "load" });
        const result = await coordinateThreeMfAuthorization(page, {
          instanceId: "123", authorizationTimeout: 3000, verificationTimeoutMs: 2000,
          inputAutoVerify: true, inputAutoVerifyChallenges: true,
          authorizationGuardSignal: guard.signal,
        });
        await page.waitForFunction(() => window.fixtureSignedUrlReceived === true, { polling: 25, timeout: 1000 });
        assert.deepEqual(await page.evaluate(() => ({
          clicks: window.fixtureClicks,
          trusted: window.fixtureTrustedClick,
          received418: window.fixtureReceived418,
          receivedCaptcha: window.fixtureReceivedCaptcha,
          sdkLoaded: window.fixtureSdkLoaded,
          challengeMounted: !!document.querySelector(".geetest_box"),
        })), { clicks: 1, trusted: true, received418: true, receivedCaptcha: true, sdkLoaded: true, challengeMounted: false });
        assert.deepEqual(authorizationMethods, ["GET", "OPTIONS", "GET"]);
        assert.equal(sdkRequests, 1);
        assert.equal(guard.signal.aborted, false);
        assert.equal(result.status_code, 200);
        assert.equal(result.payload.url, "https://download.example.test/fixture.3mf");
        assert.equal(result.verification.completed, true);
        assert.equal(result.authorization_diagnostics.stage, "page_response");
      } finally {
        await page.setRequestInterception(false);
        await guard.dispose();
      }
    });
  } finally {
    if (browser) await browser.close();
    if (server) await new Promise(resolve => server.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
});
