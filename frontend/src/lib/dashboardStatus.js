const VERIFICATION_STATES = new Set(["verification_required", "cloudflare", "auth_required"]);

export function needsBrowserVerification(item) {
  if (!item || !item.key) {
    return false;
  }
  const checks = Array.isArray(item.checks) ? item.checks : [];
  return item.action_label === "去验证" && checks.some((check) => VERIFICATION_STATES.has(String(check?.state || "")));
}

export function dashboardStatusElementKind(_item) {
  return "div";
}

export function dashboardStatusAction(item) {
  if (needsBrowserVerification(item)) {
    return {
      kind: "browser-verification",
      label: item.action_label || "去验证",
    };
  }
  if (item?.route && item?.action_label) {
    return {
      kind: "route",
      label: item.action_label,
      to: item.route,
    };
  }
  if (item?.url && item?.action_label) {
    return {
      kind: "external",
      label: item.action_label,
      href: item.url,
    };
  }
  return null;
}
