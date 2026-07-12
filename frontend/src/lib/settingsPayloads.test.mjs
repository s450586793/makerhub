import assert from "node:assert/strict";
import { test } from "node:test";
import * as settingsPayloads from "./settingsPayloads.js";

import {
  buildAdvancedPayload,
  buildProxyPayload,
  buildRuntimePayload,
  buildSharingPayload,
  createResponseGuard,
  normalizeBoundedInt,
  normalizeDailyThreeMfLimit,
} from "./settingsPayloads.js";

test("token response guard rejects a response after the dialog closes", async () => {
  assert.equal(typeof createResponseGuard, "function");
  const guard = createResponseGuard();
  const isCurrent = guard.begin();
  guard.invalidate();

  await Promise.resolve();

  assert.equal(isCurrent(), false);
});

test("normalize bounded integers clamps invalid and out-of-range values", () => {
  assert.equal(normalizeBoundedInt("bad", 2, 1, 4), 2);
  assert.equal(normalizeBoundedInt(0, 2, 1, 4), 1);
  assert.equal(normalizeBoundedInt(99, 2, 1, 4), 4);
  assert.equal(normalizeBoundedInt(3, 2, 1, 4), 3);
});

test("daily 3MF limits allow zero and preserve large configured values", () => {
  assert.equal(normalizeDailyThreeMfLimit("", 100), 100);
  assert.equal(normalizeDailyThreeMfLimit(0, 100), 0);
  assert.equal(normalizeDailyThreeMfLimit(9999, 100), 9999);
});

test("runtime payload clamps process settings", () => {
  assert.deepEqual(
    buildRuntimePayload({ web_workers: 99, worker_concurrency: 0 }),
    { web_workers: 8, worker_concurrency: 1 },
  );
});

test("advanced payload normalizes engine and worker limits", () => {
  assert.deepEqual(
    buildAdvancedPayload({
      remote_refresh_model_workers: 99,
      makerworld_request_limit: 0,
      comment_asset_download_limit: "bad",
      three_mf_download_limit: 2,
      disk_io_limit: 8,
    }),
    {
      scraping_engine: "scrapling_first",
      remote_refresh_model_workers: 4,
      makerworld_request_limit: 1,
      comment_asset_download_limit: 4,
      three_mf_download_limit: 2,
      disk_io_limit: 4,
    },
  );
});

test("proxy and sharing payloads copy mutable form data safely", () => {
  const proxyForm = { enabled: true, http_proxy: "http://127.0.0.1:7890", https_proxy: "" };
  assert.deepEqual(buildProxyPayload(proxyForm), proxyForm);

  const sharingForm = {
    public_base_url: "https://example.test",
    default_expires_days: 999,
    include_images: true,
    include_model_files: false,
    model_file_types: ["3mf"],
    include_attachments: true,
    attachment_file_types: ["pdf"],
    include_comments: false,
  };
  const payload = buildSharingPayload(sharingForm);

  assert.equal(payload.default_expires_days, 90);
  assert.deepEqual(payload.model_file_types, ["3mf"]);
  assert.notEqual(payload.model_file_types, sharingForm.model_file_types);
});

test("token lists retain metadata but discard every plaintext and hash field", () => {
  assert.equal(typeof settingsPayloads.normalizeTokenItems, "function");
  const items = settingsPayloads.normalizeTokenItems([
    {
      id: "token-1",
      name: "CI",
      token_prefix: "mht_example",
      token_value: "mht_example_plaintext",
      token: "mht_other_plaintext",
      token_hash: "secret-hash",
      permissions: ["archive_write"],
      status: "active",
      created_at: "2026-07-12T10:00:00+08:00",
      expires_at: "",
      last_used_at: "",
      disabled: false,
      revoked_at: "",
    },
  ]);

  assert.deepEqual(items, [
    {
      id: "token-1",
      name: "CI",
      token_prefix: "mht_example",
      permissions: ["archive_write"],
      status: "active",
      created_at: "2026-07-12T10:00:00+08:00",
      expires_at: "",
      last_used_at: "",
      disabled: false,
      revoked_at: "",
    },
  ]);
  assert.equal(JSON.stringify(items).includes("plaintext"), false);
  assert.equal(JSON.stringify(items).includes("secret-hash"), false);
});
