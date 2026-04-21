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

export function normalizeSubscriptionsPayload(response = {}) {
  return {
    items: Array.isArray(response.items) ? response.items : [],
    count: Number(response.count || 0),
    summary: {
      enabled: Number(response.summary?.enabled || 0),
      running: Number(response.summary?.running || 0),
      deleted_marked: Number(response.summary?.deleted_marked || 0),
    },
    sections: Array.isArray(response.sections) ? response.sections : [],
    settings: mergeSubscriptionSettings(response.settings || {}),
  };
}

export function subscriptionModeLabel(mode) {
  if (mode === "author_upload") {
    return "作者页";
  }
  return "合集 / 收藏夹";
}
