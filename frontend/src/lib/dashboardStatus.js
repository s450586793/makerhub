export function dashboardStatusElementKind(_item) {
  return "div";
}

export function dashboardStatusAction(item) {
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
