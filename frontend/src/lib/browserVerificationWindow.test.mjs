import assert from "node:assert/strict";
import { test } from "node:test";

import {
  browserVerificationPath,
  closeBrowserVerificationWindow,
  navigateBrowserVerificationWindow,
  openBrowserVerificationWindow,
  reserveBrowserVerificationWindow,
} from "./browserVerificationWindow.js";

test("browser verification path encodes session id", () => {
  assert.equal(browserVerificationPath("bv_a/b"), "/browser-verification/bv_a%2Fb");
});

test("browser verification opens a focused lightweight popup", () => {
  const calls = [];
  const popup = {
    focused: false,
    location: { href: "about:blank" },
    focus() {
      this.focused = true;
    },
  };
  const fakeWindow = {
    open(path, name, features) {
      calls.push({ path, name, features });
      return popup;
    },
  };

  assert.equal(openBrowserVerificationWindow("bv_123", fakeWindow), true);
  assert.deepEqual(calls, [
    {
      path: "about:blank",
      name: "makerhub-3mf-verification",
      features: "popup=yes,width=1120,height=820,left=80,top=60",
    },
  ]);
  assert.equal(popup.location.href, "/browser-verification/bv_123");
  assert.equal(popup.focused, true);
});

test("browser verification can reserve popup before async session creation", () => {
  const calls = [];
  const popup = {
    closed: false,
    focused: false,
    location: { href: "about:blank" },
    document: {
      title: "",
      body: { innerHTML: "" },
    },
    close() {
      this.closed = true;
    },
    focus() {
      this.focused = true;
    },
  };
  const fakeWindow = {
    open(path, name, features) {
      calls.push({ path, name, features });
      return popup;
    },
  };

  const reserved = reserveBrowserVerificationWindow(fakeWindow);
  assert.equal(reserved, popup);
  assert.deepEqual(calls[0], {
    path: "about:blank",
    name: "makerhub-3mf-verification",
    features: "popup=yes,width=1120,height=820,left=80,top=60",
  });
  assert.equal(popup.document.title, "3MF 验证 | makerhub");

  assert.equal(navigateBrowserVerificationWindow(reserved, "bv_456"), true);
  assert.equal(popup.location.href, "/browser-verification/bv_456");
  assert.equal(popup.focused, true);

  closeBrowserVerificationWindow(reserved);
  assert.equal(popup.closed, true);
});
