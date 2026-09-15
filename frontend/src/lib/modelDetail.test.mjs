import assert from "node:assert/strict";
import test from "node:test";
import { availableMachines, filterProfiles, nextGalleryIndex, profileMachines, profilePopoverPosition, safeExternalUrl } from "./modelDetail.js";

test("机型拆分保留带空格的名称，并按完整机型匹配", () => {
  const profiles = [{ machine: "X1 Carbon, P1S / A1" }, { machine: "A1 mini" }, { machine: "通用" }];
  assert.deepEqual(profileMachines(profiles[0]), ["X1 Carbon", "P1S", "A1"]);
  assert.deepEqual(availableMachines(profiles), ["X1 Carbon", "P1S", "A1", "A1 mini"]);
  assert.deepEqual(filterProfiles(profiles, "a1"), [profiles[0], profiles[2]]);
  assert.equal(filterProfiles(profiles), profiles);
  assert.deepEqual(filterProfiles([{ machine: "P1S" }], "X1"), []);
  assert.deepEqual(profileMachines({ machines: [{ name: "P1S" }, null, "P1S"] }), ["P1S"]);
});

test("图库索引处理首尾循环、无图片和失效索引", () => {
  assert.equal(nextGalleryIndex(0, -1, 3), 2);
  assert.equal(nextGalleryIndex(2, 1, 3), 0);
  assert.equal(nextGalleryIndex(-1, 1, 3), 1);
  assert.equal(nextGalleryIndex(0, 1, 0), -1);
  assert.equal(nextGalleryIndex(0, 1, 1), 0);
});

test("配置浮层在窄屏和底部均限制于视口内", () => {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 480 }]) {
    const result = profilePopoverPosition({ left: viewport.width - 250, top: viewport.height - 100, bottom: viewport.height - 20 }, viewport);
    assert.ok(result.left >= 16);
    assert.ok(result.left + result.width <= viewport.width - 16);
    assert.ok(result.top >= 16 && result.top < viewport.height - 16);
    assert.ok(result.maxHeight <= viewport.height - 32);
  }
});

test("源链接仅允许 HTTP(S)", () => {
  assert.equal(safeExternalUrl("javascript:alert(1)"), "");
  assert.equal(safeExternalUrl("//evil.example"), "");
  assert.equal(safeExternalUrl("https://makerworld.com.cn/zh/models/1"), "https://makerworld.com.cn/zh/models/1");
});
