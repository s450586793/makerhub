import { resolveHydratedLightPhase } from "./hydratedPageLoader.js";


export const DEFAULT_SUBSCRIPTION_SETTINGS = {
  default_cron: "0 */6 * * *",
  default_enabled: true,
  default_initialize_from_source: true,
  card_sort: "recent",
  hide_disabled_from_cards: false,
};

export function mergeSubscriptionSettings(settings = {}) {
  return {
    ...DEFAULT_SUBSCRIPTION_SETTINGS,
    ...(settings || {}),
  };
}

export function createEmptySubscriptionsPayload() {
  return {
    items: [],
    count: 0,
    summary: {
      enabled: 0,
      running: 0,
      deleted_marked: 0,
    },
    sections: [],
    settings: mergeSubscriptionSettings(),
  };
}

function normalizeSubscriptionSection(section = {}) {
  const items = Array.isArray(section.items) ? section.items : [];
  return {
    ...section,
    items,
    count: Number(section.count ?? items.length),
    total: Number(section.total ?? items.length),
    page: Number(section.page || 1),
    page_size: Number(section.page_size || items.length || 0),
    has_more: Boolean(section.has_more),
  };
}

export function normalizeSubscriptionsPayload(response = {}) {
  return {
    items: Array.isArray(response.items) ? response.items : [],
    count: Number(response.count || 0),
    summary: {
      enabled: Number(response.summary?.enabled || 0),
      running: Number(response.summary?.running || 0),
      deleted_marked: Number(response.summary?.deleted_marked || 0),
    },
    sections: Array.isArray(response.sections) ? response.sections.map(normalizeSubscriptionSection) : [],
    settings: mergeSubscriptionSettings(response.settings || {}),
    runtime: response.runtime && typeof response.runtime === "object" ? response.runtime : {},
  };
}

function cardHasFullVisuals(card = {}) {
  return Boolean(
    String(card.preview_snapshot_url || "").trim()
      || (Array.isArray(card.preview_models) && card.preview_models.length)
      || (Array.isArray(card.model_dirs) && card.model_dirs.length)
  );
}

export function shouldDeferLightSubscriptionCards({ hydrateFull = false, currentSection = {}, displaySection = {} } = {}) {
  const currentItems = Array.isArray(currentSection?.items) ? currentSection.items : [];
  return !resolveHydratedLightPhase({
    hydrateFull,
    incomingItems: displaySection?.items || [],
    hasStableView: currentItems.some((item) => cardHasFullVisuals(item)),
  }).renderLight;
}

export function mergeSubscriptionSourcesForLightRefresh(currentSection = {}, lightSection = {}) {
  const currentItems = Array.isArray(currentSection?.items) ? currentSection.items : [];
  const lightItems = Array.isArray(lightSection?.items) ? lightSection.items : [];
  if (!currentItems.length || !lightItems.length) {
    return {
      ...(lightSection || {}),
      items: lightItems,
      count: lightItems.length,
    };
  }
  const currentByKey = new Map(
    currentItems
      .map((item) => [String(item?.key || "").trim(), item])
      .filter(([key]) => Boolean(key)),
  );
  const items = lightItems.map((item) => {
    const key = String(item?.key || "").trim();
    const currentItem = key ? currentByKey.get(key) : null;
    if (!currentItem || !cardHasFullVisuals(currentItem)) {
      return item;
    }
    return {
      ...item,
      preview_models: currentItem.preview_models,
      preview_snapshot_url: currentItem.preview_snapshot_url,
      cover_url: currentItem.cover_url || item.cover_url,
      avatar_url: currentItem.avatar_url || item.avatar_url,
      model_dirs: currentItem.model_dirs,
      model_count: currentItem.model_count,
      local_model_count: currentItem.local_model_count,
      stats: currentItem.stats,
      recent_summary: currentItem.recent_summary,
    };
  });
  return {
    ...(lightSection || {}),
    items,
    count: items.length,
  };
}

export function subscriptionModeLabel(mode) {
  if (mode === "author_upload") {
    return "作者页";
  }
  return "合集 / 收藏夹";
}
