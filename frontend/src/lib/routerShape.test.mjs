import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("browser verification route is removed", () => {
  const routerSource = readFileSync(new URL("../router.js", import.meta.url), "utf8");
  const dashboardSource = readFileSync(new URL("../pages/DashboardPage.vue", import.meta.url), "utf8");
  const routeToken = ["browser", "verification"].join("-");
  const pageToken = ["Browser", "Verification", "Page"].join("");
  const bodyClassToken = `${routeToken}-page`;
  const windowToken = ["browser", "Verification", "Window"].join("");
  const startToken = ["start", "Browser", "Verification", "FromCard"].join("");
  const apiToken = ["api", routeToken, "sessions"].join("/");

  assert.equal(routerSource.includes(routeToken), false);
  assert.equal(routerSource.includes(pageToken), false);
  assert.equal(routerSource.includes(bodyClassToken), false);
  assert.equal(dashboardSource.includes(windowToken), false);
  assert.equal(dashboardSource.includes(startToken), false);
  assert.equal(dashboardSource.includes(apiToken), false);
});
