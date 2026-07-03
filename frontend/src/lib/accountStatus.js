function cleanText(value) {
  return String(value || "").trim();
}

function platformShortLabel(platform) {
  return platform === "global" ? "国际" : "国内";
}

function hasProfileEvidence(item = {}) {
  return Boolean(
    cleanText(item.display_name || item.name || item.account_name)
    || cleanText(item.account_id || item.uid || item.account_uid)
    || cleanText(item.handle || item.account_handle)
    || cleanText(item.avatar_url || item.account_avatar_url),
  );
}

function hasSourceEvidence(sourceInventory = {}, sourceSync = {}) {
  const lastStatus = cleanText(sourceSync.last_status || sourceInventory.last_status);
  return ["success", "warning", "pending"].includes(lastStatus)
    || Boolean(cleanText(sourceSync.last_sync_at || sourceInventory.last_sync_at))
    || hasProfileEvidence(sourceInventory.account)
    || hasProfileEvidence(sourceSync);
}

function displayStatus(item, context = {}) {
  const status = cleanText(item?.status);
  if (
    (status === "http_error" || status === "html_response")
    && (hasProfileEvidence(item) || hasSourceEvidence(context.sourceInventory, context.sourceSync))
  ) {
    return "";
  }
  return status;
}

export function accountStatusLabel(item, context = {}) {
  const status = displayStatus(item, context);
  if (status === "ok") return "正常";
  if (status === "auth_required") return "Cookie 失效";
  if (status === "verification_required") return "需要验证";
  if (status === "html_response") return "读取受限";
  if (status === "http_error") return "连接异常";
  if (status) return "需检查";
  return "已保存";
}

export function accountStatusClass(item, context = {}) {
  const status = displayStatus(item, context);
  if (!status || status === "ok") return "";
  if (status === "html_response") return "is-warning";
  return "is-expired";
}

export function accountMessageText(item, context = {}) {
  const platformLabel = platformShortLabel(item?.platform);
  const rawStatus = cleanText(item?.status);
  const status = displayStatus(item, context);
  const raw = cleanText(item?.message);
  if (raw) {
    if ((rawStatus === "http_error" || rawStatus === "html_response") && !status) {
      return `${platformLabel}账号已保存，账号资料或来源同步可读取。`;
    }
    if (/Cookie\s*部分成功/.test(raw) || /接口可访问/.test(raw) || /\b\d+\s*\/\s*\d+\b/.test(raw)) {
      return `${platformLabel}账号已保存，部分账号信息暂时读取失败；可以点击同步重试。`;
    }
    if (/基础认证可用/.test(raw) || /接口暂时未通过/.test(raw)) {
      return `${platformLabel}账号已保存，部分账号信息暂时读取失败；可以点击同步重试。`;
    }
    if (/认证接口可正常访问/.test(raw)) {
      return `${platformLabel}账号可用，Cookie 已保存。`;
    }
    if (/认证接口返回了登录页或网页页面/.test(raw)) {
      return `${platformLabel}账号已保存，但暂时无法读取账号信息；可以点击同步重试。`;
    }
    return raw;
  }
  return status === "ok" ? `${platformLabel}账号可用，Cookie 已保存。` : "已保存账号，建议测试一次。";
}
