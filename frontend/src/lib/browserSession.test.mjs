import assert from "node:assert/strict";
import { test } from "node:test";

import {
  browserSessionBusy,
  browserSessionMessage,
  browserSessionStatusClass,
  browserSessionStatusLabel,
  resolveCloakBrowserPublicUrl,
  shouldShowBrowserSession,
} from "./browserSession.js";

test("browser session status maps operational states", () => {
  assert.equal(browserSessionStatusLabel({ browser_status: "synced" }), "浏览器已同步");
  assert.equal(browserSessionStatusClass({ browser_status: "synced" }), "");
  assert.equal(browserSessionStatusClass({ browser_status: "action_required" }), "is-expired");
  assert.equal(browserSessionStatusClass({ browser_status: "waiting" }), "is-warning");
  assert.equal(browserSessionBusy({ browser_status: "syncing" }), true);
  assert.equal(browserSessionBusy({ browser_status: "waiting" }), false);
});

test("browser session message has a stable fallback", () => {
  assert.equal(browserSessionMessage({ browser_message: "已同步" }), "已同步");
  assert.match(browserSessionMessage({}), /自动把 Cookie 同步/);
});

test("unlinked browser is hidden for an archive-ready account", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "none" }), false);
});

test("browser state is shown when verification requires browser recovery", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "browser" }), true);
});

test("public URL uses configured value or current host port 9050", () => {
  assert.equal(
    resolveCloakBrowserPublicUrl("https://browser.example.test"),
    "https://browser.example.test",
  );
  assert.equal(
    resolveCloakBrowserPublicUrl("", { protocol: "http:", hostname: "nas.local" }),
    "http://nas.local:9050/",
  );
});
