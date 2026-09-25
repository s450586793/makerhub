import assert from "node:assert/strict";
import { test } from "node:test";
import puppeteer from "puppeteer-core";

import { coordinateThreeMfAuthorization } from "../../../app/services/cloakbrowser_bridge.mjs";

const executablePath = process.env.MAKERHUB_TEST_CHROMIUM;

test("3MF browser interactions use local fixtures without real download requests", {
  skip: !executablePath && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ["--no-sandbox", "--disable-background-networking"],
  });
  t.after(() => browser.close());

  async function fixture(t, html) {
    const page = await browser.newPage();
    t.after(() => page.close());
    const requests = [];
    await page.setRequestInterception(true);
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/instance/123/f3mf")) {
        requests.push(url.pathname);
        void request.respond({ status: 200, contentType: "application/json", body: JSON.stringify({
          name: "fixture.3mf", url: "https://download.example.test/fixture.3mf",
        }) });
      } else if (request.isNavigationRequest()) {
        void request.respond({ status: 200, contentType: "text/html", body: "<html><body></body></html>" });
      } else {
        void request.abort();
      }
    });
    await page.goto("https://makerworld.com.cn/zh/models/456#profileId-123");
    await page.setContent(html);
    return { page, requests };
  }

  const download = `<span class="primaryButton" style="display:inline-block;padding:12px"
    onclick="fetch('/api/v1/design-service/instance/123/f3mf')"><span>下载 3MF</span></span>`;
  const authorize = page => coordinateThreeMfAuthorization(page, { instanceId: "123", authorizationTimeout: 3000 });

  await t.test("tag links and their nested role buttons never steal the download click", async (t) => {
    const { page, requests } = await fixture(t, `
      <a href="/zh/search/models?keyword=tag:download"><div role="button">无 AMS 3D打印模型下载</div></a>
      <span class="primaryButton" style="display:none">下载 3MF</span>
      <div class="rich_text_show"><span class="primaryButton">Download 3MF</span></div>
      <div style="height:1500px"></div>${download}`);
    const result = await authorize(page);
    assert.equal(result.payload.name, "fixture.3mf");
    assert.equal(requests.length, 1);
    assert.equal(new URL(page.url()).pathname, "/zh/models/456");
  });

  await t.test("download does not migrate DOM handles between JavaScript worlds", async (t) => {
    const { page, requests } = await fixture(t, download);
    const client = page._client();
    const send = client.send.bind(client);
    client.send = (method, ...args) => {
      if (method === "DOM.resolveNode") {
        throw new Error("Protocol error (Runtime.callFunctionOn): Argument should belong to the same JavaScript world as target object");
      }
      return send(method, ...args);
    };
    t.after(() => { client.send = send; });
    assert.equal((await authorize(page)).payload.name, "fixture.3mf");
    assert.equal(requests.length, 1);
  });

  await t.test("an overlapping dialog prevents any download click", async (t) => {
    const { page, requests } = await fixture(t, `${download}
      <div style="position:fixed;inset:0;z-index:10;background:white">Dialog</div>`);
    await assert.rejects(authorize(page), /action is obscured/);
    assert.equal(requests.length, 0);
  });

  await t.test("a disabled download container prevents any request", async (t) => {
    const { page, requests } = await fixture(t, `<div aria-disabled="true">${download}</div>`);
    await assert.rejects(authorize(page), /action is disabled/);
    assert.equal(requests.length, 0);
  });

  await t.test("crowdfunding still finishes without authorization", async (t) => {
    const { page, requests } = await fixture(t, '<a href="/zh/crowdfunding/123-demo">查看项目</a>');
    assert.equal((await authorize(page)).payload.code, "MAKERHUB_CROWDFUNDING");
    assert.equal(requests.length, 0);
  });

  await t.test("Cloudflare interstitial returns a verification result without a download click", async (t) => {
    const { page, requests } = await fixture(t, `
      <title>请稍候…</title><form id="challenge-form">
      正在进行安全验证。本网站使用安全服务防护恶意自动程序。</form>`);
    const result = await authorize(page);
    assert.equal(result.status_code, 403);
    assert.equal(result.payload.code, "MAKERHUB_CLOUDFLARE");
    assert.equal(requests.length, 0);
  });

  await t.test("a transient Cloudflare check may finish before pausing the task", async (t) => {
    const { page, requests } = await fixture(t, `
      <title>Just a moment...</title><form id="challenge-form">Checking your browser</form>`);
    const pending = authorize(page);
    await new Promise(resolve => setTimeout(resolve, 100));
    await page.setContent(download);
    assert.equal((await pending).payload.name, "fixture.3mf");
    assert.equal(requests.length, 1);
  });

  await t.test("Cloudflare mentioned in a model description does not block downloads", async (t) => {
    const { page, requests } = await fixture(t, `
      <title>Example model</title><div class="rich_text_show">Cloudflare: Just a moment...</div>${download}`);
    assert.equal((await authorize(page)).payload.name, "fixture.3mf");
    assert.equal(requests.length, 1);
  });
});
