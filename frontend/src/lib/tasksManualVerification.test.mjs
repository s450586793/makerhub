import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { compile, NodeTypes, parse as parseTemplate } from "@vue/compiler-dom";
import { babelParse, parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";
import * as Vue from "vue";
import { createMemoryHistory, createRouter, RouterLink } from "vue-router";

const source = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");
const { descriptor } = parse(source);
const helperNames = [
  "getMissingKey", "isMissingActionBusy", "isRetryLocked", "retryLabel",
  "needsManualVerification", "needsRedemption", "sourceHomepageForMissingItem", "missingVerificationHref",
  "missingActionHint", "formatMissingStatus",
];
const pageFunctions = babelParse(descriptor.scriptSetup.content, { sourceType: "module" }).program.body
  .filter((node) => node.type === "FunctionDeclaration" && helperNames.includes(node.id.name))
  .map((node) => descriptor.scriptSetup.content.slice(node.start, node.end));
const helpers = new Function("pendingMissingActionKey", `
  ${pageFunctions.join("\n")}
  return { ${helperNames.join(", ")} };
`)(Vue.ref(""));

function elements(node, predicate) {
  return [
    ...(node.type === NodeTypes.ELEMENT && predicate(node) ? [node] : []),
    ...(node.children || []).flatMap((child) => elements(child, predicate)),
  ];
}

function attribute(node, name) {
  return node.props.find((prop) => prop.type === NodeTypes.ATTRIBUTE && prop.name === name)?.value?.content;
}

const [actionsTemplate] = elements(parseTemplate(descriptor.template.content), (node) => (
  attribute(node, "class")?.split(/\s+/).includes("missing-actions")
));
assert.ok(actionsTemplate, "the missing 3MF row must contain its action controls");
const render = new Function("Vue", compile(actionsTemplate.loc.source, {
  mode: "function",
  prefixIdentifiers: true,
}).code)(Vue);

async function renderActionControls(item) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ["/", "/settings"].map((path) => ({ path, component: { render: () => null } })),
  });
  await router.push("/");
  const app = Vue.createSSRApp({
    components: { RouterLink },
    setup: () => ({ item, missingStatus: "", ...helpers }),
    render,
  });
  app.use(router);
  const html = await renderToString(app);
  return elements(parseTemplate(html), (node) => node.tag === "a" || node.tag === "button");
}

async function renderActions(item) {
  return (await renderActionControls(item)).filter((node) => node.tag === "a");
}

function linkText(node) {
  return node.children.map((child) => child.content || "").join("").trim();
}

const modelUrl = "https://makerworld.com.cn/zh/models/123-example";
const manualStatuses = ["verification_required", "cloudflare", "auth_required", "cookie_invalid"];

for (const status of manualStatuses) {
  test(`${status} directs account recovery to the internal settings route`, async () => {
    const links = await renderActions({ status, model_url: modelUrl, source: "cn" });
    const accountLink = links.find((node) => linkText(node) === "处理账号验证");
    assert.ok(accountLink, "manual recovery must have an account settings action");
    assert.equal(attribute(accountLink, "href"), "/settings?tab=accounts");
    assert.equal(attribute(accountLink, "target"), undefined);
    assert.match(attribute(accountLink, "class"), /\bbutton-primary\b/);
    assert.equal(attribute(accountLink, "title"), helpers.missingActionHint({ status }));
  });
}

test("the source page remains a secondary external browsing action", async () => {
  const links = await renderActions({ status: "verification_required", model_url: modelUrl, source: "cn" });
  const sourceLink = links.find((node) => linkText(node) === "访问源页面");
  assert.ok(sourceLink);
  assert.equal(attribute(sourceLink, "href"), modelUrl);
  assert.equal(attribute(sourceLink, "target"), "_blank");
  assert.match(attribute(sourceLink, "rel"), /\bnoopener\b/);
  assert.match(attribute(sourceLink, "rel"), /\bnoreferrer\b/);
  assert.match(attribute(sourceLink, "class"), /\bbutton-secondary\b/);
  assert.doesNotMatch(attribute(sourceLink, "class"), /\bbutton-primary\b/);
});

test("source browsing falls back to the model page before the platform homepage", async () => {
  for (const [platform, homepage] of [["cn", "https://makerworld.com.cn"], ["global", "https://makerworld.com"]]) {
    const links = await renderActions({ status: "verification_required", source: platform });
    assert.equal(attribute(links.find((node) => linkText(node) === "访问源页面"), "href"), homepage);
    const modelLinks = await renderActions({ status: "missing", model_id: "132602", source: platform });
    assert.equal(attribute(modelLinks.find((node) => linkText(node) === "访问源页面"), "href"), `${homepage}/zh/models/132602`);
    assert.equal(helpers.missingVerificationHref({ model_id: "not-a-model", source: platform }), homepage);
  }
});

test("tasks that do not need account verification omit the recovery entry", async () => {
  for (const status of ["missing", "http_error", "download_limited", "not_found", "queued", "running", "failed", "completed", ""]) {
    const links = await renderActions({ status, model_url: modelUrl, source: "cn" });
    assert.equal(links.some((node) => attribute(node, "href") === "/settings?tab=accounts"), false);
    assert.equal(links.some((node) => linkText(node) === "处理账号验证"), false);
    const sourceLink = links.find((node) => linkText(node) === "访问源页面");
    assert.ok(sourceLink, `${status || "unknown"} tasks must expose the source page`);
    assert.equal(attribute(sourceLink, "href"), modelUrl);
    assert.equal(attribute(sourceLink, "target"), "_blank");
    assert.match(attribute(sourceLink, "rel"), /\bnoopener\b/);
  }
});

test("recovery guidance uses the linked browser and waits for the download check", () => {
  for (const status of manualStatuses) {
    const hint = helpers.missingActionHint({ status });
    assert.match(hint, /账号设置/);
    assert.match(hint, /关联.*浏览器/);
    assert.match(hint, /从浏览器同步/);
    assert.match(hint, /等待.*下载状态检查/);
    assert.doesNotMatch(hint, /cf_clearance|Cookie|token|回到 MakerHub 重试|验证后.*重试/i);
  }
});

test("task status labels distinguish Cloudflare from manual verification", () => {
  assert.equal(helpers.formatMissingStatus("verification_required"), "需要验证");
  assert.equal(helpers.formatMissingStatus("cloudflare"), "Cloudflare 校验");
});

test("redemption-required tasks show their source page and allow a manual download retry", async () => {
  const controls = await renderActionControls({ status: "redemption_required", model_url: modelUrl, source: "cn" });
  const sourceLink = controls.find((node) => node.tag === "a" && linkText(node) === "前往兑换");
  assert.equal(helpers.formatMissingStatus("redemption_required"), "待兑换");
  assert.equal(helpers.needsManualVerification({ status: "redemption_required" }), false);
  assert.ok(sourceLink, "redemption must link to the model's source page");
  assert.equal(attribute(sourceLink, "href"), modelUrl);
  assert.equal(attribute(sourceLink, "target"), "_blank");
  assert.match(attribute(sourceLink, "rel"), /\bnoopener\b/);
  assert.equal(controls.some((node) => attribute(node, "href") === "/settings?tab=accounts"), false);
  const retryButton = controls.find((node) => node.tag === "button" && linkText(node) === "重新下载");
  assert.ok(retryButton, "the user must be able to retry after redeeming on MakerWorld");
  assert.equal(retryButton.props.some((prop) => prop.name === "disabled"), false);
});
