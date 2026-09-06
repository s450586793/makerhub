import assert from "node:assert/strict";
import { test } from "node:test";

import {
  browserSessionBusy,
  hasRecoveredBrowserSession,
  browserSessionMessage,
  browserSessionStatusClass,
  browserSessionStatusLabel,
  navigateCloakBrowserPopup,
  resolveCloakBrowserPublicUrl,
  shouldShowBrowserSession,
} from "./browserSession.js";

test("recovered browser session is matched to the failed platform", () => {
  const payload = {
    cookies: [
      { platform: "cn", browser_status: "waiting" },
      { platform: "global", browser_status: "synced", browser_message: "指纹浏览器登录态已同步。" },
    ],
  };

  assert.equal(hasRecoveredBrowserSession(payload, "cn"), false);
  assert.equal(hasRecoveredBrowserSession(payload, "global"), true);
});

test("missing platform or browser session is not treated as recovered", () => {
  assert.equal(hasRecoveredBrowserSession({ cookies: [] }, "cn"), false);
  assert.equal(hasRecoveredBrowserSession({ cookies: [{ platform: "cn", browser_status: "synced" }] }, ""), false);
});

test("a preserved synced status with a current browser error is not recovered", () => {
  const payload = {
    cookies: [{
      platform: "cn",
      browser_status: "synced",
      browser_message: "指纹浏览器服务暂时不可用：Network.enable timed out.",
    }],
  };

  assert.equal(hasRecoveredBrowserSession(payload, "cn"), false);
});

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
  assert.match(browserSessionMessage({}), /指纹浏览器/);
});

test("unlinked browser is hidden for an archive-ready account", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "none" }), false);
});

test("current browser warning is shown even when operational health is stale", () => {
  assert.equal(shouldShowBrowserSession({
    browser_profile_id: "profile-global",
    browser_status: "action_required",
  }, {
    state: "ok",
    action: "none",
  }), true);
});

test("browser state is shown when verification requires browser recovery", () => {
  assert.equal(shouldShowBrowserSession({}, { action: "browser" }), true);
});

test("unlinked browser is visible for a manual compatibility account", () => {
  assert.equal(browserSessionStatusLabel({ browser_status: "not_linked" }), "未关联浏览器");
  assert.equal(shouldShowBrowserSession({ browser_status: "not_linked" }, { action: "none" }), true);
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

test("deferred browser popup navigates before severing its opener", () => {
  const events = [];
  let openerAttached = true;
  const popup = {
    closed: false,
    location: {
      replace(url) {
        assert.equal(openerAttached, true);
        events.push(["replace", url]);
      },
    },
    set opener(value) {
      openerAttached = value !== null;
      events.push(["opener", value]);
    },
  };
  const windowLike = {
    open() {
      assert.fail("the existing popup should be reused");
    },
  };

  assert.equal(
    navigateCloakBrowserPopup(popup, "http://browser.example.test/", windowLike),
    true,
  );
  assert.deepEqual(events, [
    ["replace", "http://browser.example.test/"],
    ["opener", null],
  ]);
});

test("deferred browser popup falls back to a protected new window", () => {
  const openCalls = [];
  const windowLike = {
    open(...args) {
      openCalls.push(args);
      return null;
    },
  };

  assert.equal(
    navigateCloakBrowserPopup(null, "http://browser.example.test/", windowLike),
    true,
  );
  assert.deepEqual(openCalls, [[
    "http://browser.example.test/",
    "_blank",
    "noopener,noreferrer",
  ]]);
});

test("failed placeholder navigation closes it before using the fallback", () => {
  let closed = false;
  const popup = {
    closed: false,
    location: {
      replace() {
        throw new DOMException("cross-origin navigation denied");
      },
    },
    close() {
      closed = true;
    },
  };
  const windowLike = { open: () => null };

  assert.equal(
    navigateCloakBrowserPopup(popup, "http://browser.example.test/", windowLike),
    true,
  );
  assert.equal(closed, true);
});

test("browser popup navigation reports an unavailable window API", () => {
  assert.equal(
    navigateCloakBrowserPopup(null, "http://browser.example.test/", {}),
    false,
  );
});
