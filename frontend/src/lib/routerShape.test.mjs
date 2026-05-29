import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("browser verification route uses standalone light shell", () => {
  const source = readFileSync(new URL("../router.js", import.meta.url), "utf8");
  const standaloneRoutePattern = /path:\s*"\/browser-verification\/:sessionId"[\s\S]*?name:\s*"browser-verification"[\s\S]*?bodyClass:\s*"browser-verification-page"/;
  const shellChildrenBlock = source.match(/path:\s*"\/"[\s\S]*?children:\s*\[([\s\S]*?)\n\s*\],\n\s*},\n\];/)?.[1] || "";

  assert.match(source, standaloneRoutePattern);
  assert.doesNotMatch(shellChildrenBlock, /name:\s*"browser-verification"/);
});
