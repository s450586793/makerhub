import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parse as babelParse } from "@babel/parser";
import { parse } from "@vue/compiler-sfc";
import { computed, nextTick, ref } from "vue";

import { verificationImageUrl, verificationKeyboardInput, verificationPointerPoint } from "./manualVerification.js";

const source = readFileSync(new URL("../components/AccountVerificationDialog.vue", import.meta.url), "utf8");
const script = parse(source).descriptor.scriptSetup.content;
const setup = babelParse(script, { sourceType: "module" }).program.body
  .filter((node) => node.type !== "ImportDeclaration")
  .map((node) => script.slice(node.start, node.end)).join("\n");

function dialog() {
  const calls = [];
  const emitted = [];
  let changed;
  let watcher;
  let unmount;
  const props = { visible: true, platform: "cn", action: { endpoint: "/api/config/online-accounts/cn/verification" } };
  const controller = {
    open: (action) => calls.push({ operation: "open", action }),
    close: () => calls.push({ operation: "close" }),
    sendEvents: (events) => calls.push({ operation: "input", events }),
    finish: () => calls.push({ operation: "finish" }),
    refresh: () => calls.push({ operation: "refresh" }),
  };
  const dependencies = {
    computed, ref, nextTick, verificationImageUrl, verificationKeyboardInput, verificationPointerPoint,
    watch: (_source, callback) => { watcher = callback; },
    onBeforeUnmount: (callback) => { unmount = callback; },
    defineProps: () => props,
    defineEmits: () => (...event) => emitted.push(event),
    apiRequest: () => assert.fail("the dialog should delegate networking to its controller"),
    createManualVerificationController: (options) => { changed = options.onChange; return controller; },
    document: { activeElement: null, addEventListener() {}, removeEventListener() {} },
  };
  const page = new Function(...Object.keys(dependencies), setup + `
    return { state, canInteract, viewport, remoteImage, panel, closeButton, text, inputError, zoom, panMode, imageStyle, setZoom, handlePointerDown, handlePointerMove, handlePointerUp, handleWheel, handleRemoteKeydown, handlePaste, sendText, finish, handleDialogKeydown };
  `)(...Object.values(dependencies));
  const captured = [];
  const released = [];
  let focused = 0;
  page.viewport.value = { scrollLeft: 0, scrollTop: 0, scrollWidth: 1200, scrollHeight: 800, clientWidth: 600, clientHeight: 400, focus() { focused++; }, setPointerCapture: (id) => captured.push(id), releasePointerCapture: (id) => released.push(id) };
  page.remoteImage.value = { getBoundingClientRect: () => ({ left: 20, top: 30, width: 600, height: 400 }) };
  const setState = (patch = {}) => changed({ session_id: "manual_cn", platform: "cn", status: "challenge", image: "aQ==", width: 1200, height: 800, completed: false, ...patch });
  setState();
  return { ...page, calls, emitted, captured, released, props, setState, focusCount: () => focused, watcher: (...args) => watcher(...args), unmount: () => unmount() };
}

function pointer(patch = {}) {
  return { pointerId: 1, pointerType: "touch", button: 0, clientX: 320, clientY: 230, timeStamp: 100, preventDefault() {}, ...patch };
}

test("dialog forwards a touch drag in viewport coordinates and releases pointer capture once", () => {
  const page = dialog();
  page.handlePointerDown(pointer());
  page.handlePointerMove(pointer({ pointerId: 2 }));
  page.handlePointerMove(pointer({ clientX: 400, clientY: 250, timeStamp: 108 }));
  page.handlePointerMove(pointer({ clientX: 500, clientY: 250, timeStamp: 120 }));
  page.handlePointerUp(pointer({ clientX: 700, clientY: 250, timeStamp: 160 }));
  page.handlePointerUp(pointer());
  assert.deepEqual(page.calls.map((call) => call.events), [
    [{ type: "pointerdown", x: 600, y: 400, delay_ms: 0 }],
    [{ type: "pointermove", x: 960, y: 440, delay_ms: 20 }],
    [{ type: "pointerup", x: 1199, y: 440, delay_ms: 40 }],
  ]);
  assert.deepEqual(page.captured, [1]);
  assert.deepEqual(page.released, [1]);
  assert.equal(page.focusCount(), 1);
});

test("finishing a held drag sends pointerup before the finish operation", () => {
  const page = dialog();
  page.handlePointerDown(pointer());
  page.finish();
  assert.equal(page.calls[1].events[0].type, "pointerup");
  assert.deepEqual({ x: page.calls[1].events[0].x, y: page.calls[1].events[0].y }, { x: 600, y: 400 });
  assert.ok(page.calls[1].events[0].delay_ms >= 0 && page.calls[1].events[0].delay_ms <= 50);
  assert.deepEqual(page.calls[2], { operation: "finish" });
});

test("zoom centers the enlarged image and moving the picture never sends remote pointer events", async () => {
  const page = dialog();
  await page.setZoom(2);
  assert.equal(page.imageStyle.value.width, "1200px");
  assert.equal(page.viewport.value.scrollLeft, 300);
  assert.equal(page.viewport.value.scrollTop, 200);
  page.panMode.value = true;
  page.handlePointerDown(pointer());
  page.handlePointerMove(pointer({ clientX: 220, clientY: 180 }));
  page.handlePointerUp(pointer({ clientX: 220, clientY: 180 }));
  assert.equal(page.viewport.value.scrollLeft, 400);
  assert.equal(page.viewport.value.scrollTop, 250);
  assert.equal(page.calls.length, 0);
  assert.deepEqual(page.captured, [1]);
  assert.deepEqual(page.released, [1]);
  await page.setZoom(1);
  assert.equal(page.panMode.value, false);
  assert.deepEqual(page.imageStyle.value, {});
});

test("an enlarged scrolled picture maps remote clicks from its current bounds", async () => {
  const page = dialog();
  await page.setZoom(2);
  page.remoteImage.value = { getBoundingClientRect: () => ({ left: -280, top: -170, width: 1200, height: 800 }) };
  page.handlePointerDown(pointer());
  assert.deepEqual(page.calls[0].events, [{ type: "pointerdown", x: 600, y: 400, delay_ms: 0 }]);
});

test("terminal session states disable screenshot input", () => {
  const page = dialog();
  for (const status of ["error", "failed", "cancelled", "expired"]) {
    page.setState({ status });
    assert.equal(page.canInteract.value, false);
    page.handlePointerDown(pointer());
  }
  page.setState({ status: "completed", completed: true });
  page.handlePointerDown(pointer());
  assert.equal(page.calls.length, 0);
});

test("dialog normalizes wheel units and forwards keyboard and pasted text", () => {
  const page = dialog();
  page.handleWheel(pointer({ deltaY: 3, deltaMode: 1 }));
  let prevented = 0;
  page.handleRemoteKeydown({ key: "Enter", preventDefault() { prevented++; }, stopPropagation() {} });
  page.handlePaste({ preventDefault() { prevented++; }, clipboardData: { getData: () => "验证码" } });
  assert.deepEqual(page.calls.map((call) => call.events), [
    [{ type: "wheel", x: 600, y: 400, delta_y: 48 }],
    [{ type: "key", key: "Enter" }],
    [{ type: "text", text: "验证码" }],
  ]);
  assert.equal(prevented, 2);
  page.handlePaste({ preventDefault() {}, clipboardData: { getData: () => "a".repeat(2001) } });
  assert.match(page.inputError.value, /2000/);
  assert.equal(page.calls.length, 3);
});

test("Escape and unmount close the remote session, while composition Escape is ignored", () => {
  const page = dialog();
  page.handleDialogKeydown({ key: "Escape", isComposing: true });
  assert.equal(page.calls.length, 0);
  page.handleDialogKeydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(page.emitted, [["close"]]);
  page.unmount();
  assert.deepEqual(page.calls, [{ operation: "close" }, { operation: "close" }]);
});
