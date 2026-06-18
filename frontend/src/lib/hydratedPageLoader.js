export function resolveHydratedLightPhase({
  hydrateFull = false,
  incomingItems = [],
  hasStableView = false,
} = {}) {
  const hasIncomingItems = Array.isArray(incomingItems) && incomingItems.length > 0;
  const shouldDeferLight = Boolean(hydrateFull && hasIncomingItems && !hasStableView);
  return {
    renderLight: !shouldDeferLight,
    hydrateFull: Boolean(hydrateFull),
    hydrateImmediately: shouldDeferLight,
  };
}
