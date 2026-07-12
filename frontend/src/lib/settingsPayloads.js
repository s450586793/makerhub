export function normalizeBoundedInt(value, fallback, min, max) {
  if (value === "" || value === null || value === undefined) {
    return fallback;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.min(Math.max(Math.trunc(numeric), min), max);
}

export function normalizeDailyThreeMfLimit(value, fallback = 100) {
  if (value === "" || value === null || value === undefined) {
    return fallback;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.max(0, Math.trunc(numeric));
}

export function buildRuntimePayload(form = {}) {
  return {
    web_workers: normalizeBoundedInt(form.web_workers, 1, 1, 8),
    worker_concurrency: normalizeBoundedInt(form.worker_concurrency, 2, 1, 4),
  };
}

export function buildProxyPayload(form = {}) {
  return {
    enabled: Boolean(form.enabled),
    http_proxy: String(form.http_proxy || ""),
    https_proxy: String(form.https_proxy || ""),
  };
}

export function buildAdvancedPayload(form = {}) {
  return {
    scraping_engine: "scrapling_first",
    remote_refresh_model_workers: normalizeBoundedInt(form.remote_refresh_model_workers, 2, 1, 4),
    makerworld_request_limit: normalizeBoundedInt(form.makerworld_request_limit, 2, 1, 8),
    comment_asset_download_limit: normalizeBoundedInt(form.comment_asset_download_limit, 4, 1, 16),
    three_mf_download_limit: normalizeBoundedInt(form.three_mf_download_limit, 1, 1, 4),
    disk_io_limit: normalizeBoundedInt(form.disk_io_limit, 1, 1, 4),
  };
}

export function buildThreeMfLimitsPayload(form = {}) {
  return {
    cn_daily_limit: normalizeDailyThreeMfLimit(form.cn_daily_limit),
    global_daily_limit: normalizeDailyThreeMfLimit(form.global_daily_limit),
  };
}

export function buildSharingPayload(form = {}) {
  return {
    public_base_url: String(form.public_base_url || ""),
    default_expires_days: normalizeBoundedInt(form.default_expires_days, 7, 1, 90),
    include_images: form.include_images !== false,
    include_model_files: form.include_model_files !== false,
    model_file_types: Array.isArray(form.model_file_types) ? [...form.model_file_types] : [],
    include_attachments: form.include_attachments !== false,
    attachment_file_types: Array.isArray(form.attachment_file_types) ? [...form.attachment_file_types] : [],
    include_comments: form.include_comments !== false,
  };
}

export function normalizeTokenItems(items = []) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      id: String(item.id || ""),
      name: String(item.name || ""),
      token_prefix: String(item.token_prefix || ""),
      permissions: Array.isArray(item.permissions) ? [...item.permissions] : [],
      status: String(item.status || "active"),
      created_at: String(item.created_at || ""),
      expires_at: String(item.expires_at || ""),
      last_used_at: String(item.last_used_at || ""),
      disabled: Boolean(item.disabled),
      revoked_at: String(item.revoked_at || ""),
    }));
}
