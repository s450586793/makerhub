import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const routerSource = readFileSync(new URL("../router.js", import.meta.url), "utf8");
const appShellSource = readFileSync(new URL("../layouts/AppShell.vue", import.meta.url), "utf8");

function routeRecordSource(routeName) {
  const routeStart = routerSource.indexOf(`name: "${routeName}",`);
  assert.notEqual(routeStart, -1, `missing ${routeName} route`);
  const routeEnd = routerSource.indexOf("\n      },", routeStart);
  assert.notEqual(routeEnd, -1, `missing ${routeName} route boundary`);
  return routerSource.slice(routeStart, routeEnd);
}

test("browser verification route is removed", () => {
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

test("operational list routes are cached without caching settings details or logs", () => {
  for (const routeName of ["models", "subscriptions", "organizer", "remote-refresh", "tasks"]) {
    assert.match(routeRecordSource(routeName), /keepAlive: true/);
  }
  for (const routeName of ["settings", "model-detail", "logs"]) {
    assert.doesNotMatch(routeRecordSource(routeName), /keepAlive: true/);
  }
  assert.match(appShellSource, /<RouterView v-slot="\{ Component, route: currentRoute \}">/);
  assert.match(appShellSource, /<KeepAlive :max="5">/);
  assert.match(appShellSource, /currentRoute\.meta\.keepAlive/);
});
