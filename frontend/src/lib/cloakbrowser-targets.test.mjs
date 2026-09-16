import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import * as bridge from "../../../app/services/cloakbrowser_bridge.mjs";

async function targetFixture(t, records = [], owned = []) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "makerhub-targets-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const registryPath = path.join(directory, "targets.json");
  await writeFile(registryPath, JSON.stringify(owned));
  const context = { id: "context-1" };
  const targets = new Map(records.map((record) => [record.targetId, {
    type: "page", browserContextId: context.id, ...record,
  }]));
  const failedClose = new Set();
  const contextMetadata = { browserContextIds: ["context-2"], defaultBrowserContextId: "default-context" };
  let created = 0;
  const browser = {
    target: () => ({
      createCDPSession: async () => ({
        detach: async () => {},
        send: async (method, params = {}) => {
          if (method === "Target.getTargets") return { targetInfos: [...targets.values()] };
          if (method === "Target.getBrowserContexts") return contextMetadata;
          if (method === "Target.createTarget") {
            const targetId = `created-${++created}`;
            targets.set(targetId, { targetId, type: "page", ...params });
            return { targetId };
          }
          if (method === "Target.closeTarget") {
            if (failedClose.has(params.targetId)) return { success: false };
            targets.delete(params.targetId);
            return { success: true };
          }
          throw new Error(`unexpected CDP method: ${method}`);
        },
      }),
    }),
    waitForTarget: async (predicate) => {
      for (const record of targets.values()) {
        const target = { url: () => record.url, browserContext: () => context };
        if (predicate(target)) return target;
      }
      throw new Error("target not found");
    },
  };
  assert.equal(typeof bridge.cleanupStaleAutomationTargets, "function", "bridge must reclaim tracked targets");
  assert.equal(typeof bridge.withTemporaryTarget, "function", "bridge must track temporary target ownership");
  return { browser, context, targets, registryPath, failedClose, contextMetadata };
}

test("stale automation model pages are reclaimed without closing manual duplicates or other contexts", async (t) => {
  const model = "https://makerworld.com/en/models/2684142-example";
  const fixture = await targetFixture(t, [
    { targetId: "owned", url: model },
    { targetId: "manual", url: model },
    { targetId: "manual-api", url: "https://api.bambulab.com/v1/design-service/instance/1/f3mf" },
    { targetId: "foreign", url: model, browserContextId: "context-2" },
  ], ["owned", "foreign", "already-closed"]);

  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  assert.deepEqual([...fixture.targets.keys()], ["manual", "manual-api", "foreign"]);
  assert.deepEqual(JSON.parse(await readFile(fixture.registryPath, "utf8")), []);
});

test("temporary page ownership is persisted before navigation and removed after success", async (t) => {
  const fixture = await targetFixture(t);
  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  const result = await bridge.withTemporaryTarget(fixture.browser, fixture.context, { hidden: false }, async () => {
    assert.deepEqual(JSON.parse(await readFile(fixture.registryPath, "utf8")), ["created-1"]);
    fixture.targets.get("created-1").url = "https://makerworld.com/en/models/123";
    return { authorized: true };
  });

  assert.deepEqual(result, { authorized: true });
  assert.equal(fixture.targets.size, 0);
  assert.deepEqual(JSON.parse(await readFile(fixture.registryPath, "utf8")), []);
});

test("Chrome default context IDs are resolved when Puppeteer exposes an undefined ID", async (t) => {
  const fixture = await targetFixture(t, [
    { targetId: "owned", url: "https://makerworld.com/en/models/1", browserContextId: "default-context" },
    { targetId: "manual", url: "https://makerworld.com/en/models/1", browserContextId: "default-context" },
    { targetId: "foreign", url: "https://makerworld.com/en/models/1", browserContextId: "context-2" },
  ], ["owned", "foreign"]);
  fixture.context.id = undefined;

  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  assert.deepEqual([...fixture.targets.keys()], ["manual", "foreign"]);
});

test("temporary pages are closed when a browser operation fails", async (t) => {
  const fixture = await targetFixture(t);
  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  await assert.rejects(bridge.withTemporaryTarget(fixture.browser, fixture.context, { hidden: false }, async () => {
    throw new Error("navigation timed out");
  }), /navigation timed out/);

  assert.equal(fixture.targets.size, 0);
  assert.deepEqual(JSON.parse(await readFile(fixture.registryPath, "utf8")), []);
});

test("older Chrome default targets are distinguished from explicitly listed incognito contexts", async (t) => {
  const fixture = await targetFixture(t, [
    { targetId: "owned", url: "https://makerworld.com/en/models/1", browserContextId: "opaque-default" },
    { targetId: "manual", url: "https://makerworld.com/en/models/1", browserContextId: "opaque-default" },
    { targetId: "foreign", url: "https://makerworld.com/en/models/1", browserContextId: "context-2" },
  ], ["owned", "foreign"]);
  fixture.context.id = undefined;
  delete fixture.contextMetadata.defaultBrowserContextId;

  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  assert.deepEqual([...fixture.targets.keys()], ["manual", "foreign"]);
});

test("failed page closure remains tracked and a later operation reclaims it", async (t) => {
  const fixture = await targetFixture(t);
  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);
  fixture.failedClose.add("created-1");
  await bridge.withTemporaryTarget(fixture.browser, fixture.context, { hidden: false }, async () => {
    fixture.targets.get("created-1").url = "https://makerworld.com/en/models/123";
  });
  assert.deepEqual(JSON.parse(await readFile(fixture.registryPath, "utf8")), ["created-1"]);

  await assert.rejects(
    bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath),
    /automation.*close/i,
  );
  assert.equal(fixture.targets.size, 1);
  fixture.failedClose.clear();
  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);
  assert.equal(fixture.targets.size, 0);
});

test("an unregistered blank target left during creation is reclaimed but manual blank pages survive", async (t) => {
  const fixture = await targetFixture(t, [
    { targetId: "creation-interrupted", url: "about:blank#makerhub-123-456-abc" },
    { targetId: "manual-blank", url: "about:blank" },
  ]);

  await bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath);

  assert.deepEqual([...fixture.targets.keys()], ["manual-blank"]);
});

test("an unreadable ownership registry prevents new visible automation pages", async (t) => {
  const fixture = await targetFixture(t, [{ targetId: "manual", url: "https://makerworld.com/en" }]);
  await writeFile(fixture.registryPath, "not-json");

  await assert.rejects(
    bridge.cleanupStaleAutomationTargets(fixture.browser, fixture.context, fixture.registryPath),
    /automation.*registry/i,
  );
  assert.deepEqual([...fixture.targets.keys()], ["manual"]);
});
