import assert from "node:assert/strict";
import { access, mkdir, readdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const frontendRoot = fileURLToPath(new URL("../", import.meta.url));

async function chromiumPath() {
  const candidates = [process.env.MAKERHUB_CHROMIUM_PATH, process.env.CHROMIUM_PATH, "/usr/bin/chromium", "/usr/bin/google-chrome"];
  const cache = path.join(os.homedir(), ".cache", "ms-playwright");
  for (const name of await readdir(cache).catch(() => [])) {
    candidates.push(path.join(cache, name, "chrome-headless-shell-linux64", "chrome-headless-shell"));
    candidates.push(path.join(cache, name, "chrome-linux64", "chrome"));
  }
  for (const candidate of candidates.filter(Boolean)) {
    if (await access(candidate).then(() => true, () => false)) return candidate;
  }
  throw new Error("Set MAKERHUB_CHROMIUM_PATH to a Chromium executable");
}

const fixture = `<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app"></div>
<script type="module">
import { createApp, h } from 'vue';
import { createRouter, createMemoryHistory } from 'vue-router';
import ModelCard from '/src/components/ModelCard.vue';
import { applyTheme, startThemeObserver } from '/src/lib/theme.js';
import '/src/style.css';
const covers = [[720,720],[1200,600],[500,900]];
const cards = covers.map(([width,height], index) => {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="'+width+'" height="'+height+'"><rect width="100%" height="100%" fill="#111827"/><rect x="4%" y="4%" width="92%" height="92%" fill="none" stroke="#82c67a" stroke-width="12"/><circle cx="50%" cy="50%" r="25%" fill="#dfe7ef"/></svg>';
  return { model_dir: 'LOCAL_'+index, title: ['方形模型预览','横向成品照片','竖向成品照片'][index], source: 'local', cover_url: 'data:image/svg+xml,'+encodeURIComponent(svg), author: {name:'本地导入'}, stats:{} };
});
cards.push({model_dir:'LOCAL_empty', title:'暂无封面', source:'local', author:{name:'本地导入'}, stats:{}});
const router = createRouter({history:createMemoryHistory(), routes:[{path:'/',component:{render:()=>null}}]});
createApp({render:()=>h('div',{class:'model-grid',style:'padding:20px;gap:20px'},cards.map(model=>h(ModelCard,{model})))}).use(router).mount('#app');
window.applyTheme = applyTheme;
startThemeObserver();
applyTheme('light');
</script></body></html>`;

test("模型卡片在深浅及自动主题、桌面和手机中保留完整封面且悬浮不裁切", { timeout: 60000 }, async () => {
  const server = await createServer({
    root: frontendRoot,
    configFile: path.join(frontendRoot, "vite.config.js"),
    plugins: [{
      name: "cover-regression-fixture",
      configureServer(vite) {
        vite.middlewares.use(async (req, res, next) => {
          if (req.url === "/__cover-regression") {
            res.setHeader("Content-Type", "text/html; charset=utf-8");
            res.end(await vite.transformIndexHtml(req.url, fixture));
          } else next();
        });
      },
    }],
    server: { host: "127.0.0.1", port: 0 },
    logLevel: "error",
  });
  let browser;
  try {
    await server.listen();
    browser = await puppeteer.launch({ executablePath: await chromiumPath(), headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__cover-regression`, { waitUntil: "networkidle0" });
    assert.deepEqual(errors, []);
    await page.waitForFunction(() => document.querySelectorAll(".gallery-card__cover img").length === 3 && [...document.querySelectorAll(".gallery-card__cover img")].every((img) => img.complete && img.naturalWidth));
    for (const width of [1440, 390]) {
      await page.setViewport({ width, height: 900 });
      for (const [preference, systemTheme] of [["light", "light"], ["dark", "dark"], ["auto", "light"], ["auto", "dark"]]) {
        await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: systemTheme }]);
        await page.evaluate((theme) => window.applyTheme(theme), preference);
        await page.hover(".gallery-card");
        await new Promise((resolve) => setTimeout(resolve, 350));
        const state = await page.evaluate(() => ({
          theme: document.documentElement.dataset.theme,
          overflow: document.documentElement.scrollWidth > innerWidth,
          covers: [...document.querySelectorAll(".gallery-card__cover img")].map((img) => {
            const style = getComputedStyle(img);
            const frame = img.parentElement.getBoundingClientRect();
            const image = img.getBoundingClientRect();
            const scale = Math.min(frame.width / img.naturalWidth, frame.height / img.naturalHeight);
            return {fit:style.objectFit,transform:style.transform,frameRatio:frame.width/frame.height,
              withinFrame:image.width<=frame.width+1 && image.height<=frame.height+1,
              scaledWidth:img.naturalWidth*scale,scaledHeight:img.naturalHeight*scale,
              frameWidth:frame.width,frameHeight:frame.height,background:getComputedStyle(img.parentElement).backgroundColor};
          }),
        }));
        assert.equal(state.theme, preference === "auto" ? systemTheme : preference);
        assert.equal(state.overflow, false);
        for (const cover of state.covers) {
          assert.equal(cover.fit, "contain", "封面必须完整显示，不能把上下内容裁掉");
          assert.equal(cover.transform, "none", "悬浮不应通过放大重新裁掉封面");
          assert.ok(Math.abs(cover.frameRatio - 4/3) < 0.01, "保持卡片封面尺寸稳定");
          assert.ok(cover.withinFrame && cover.scaledWidth <= cover.frameWidth+1 && cover.scaledHeight <= cover.frameHeight+1);
          assert.notEqual(cover.background, "rgba(0, 0, 0, 0)");
        }
        if (process.env.MAKERHUB_BROWSER_ARTIFACTS_DIR) {
          await mkdir(process.env.MAKERHUB_BROWSER_ARTIFACTS_DIR, {recursive:true});
          await page.screenshot({path:path.join(process.env.MAKERHUB_BROWSER_ARTIFACTS_DIR, `covers-${preference}-${systemTheme}-${width}.png`),fullPage:true});
        }
      }
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
