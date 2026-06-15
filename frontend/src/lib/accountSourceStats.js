function cleanText(value) {
  return String(value || "").trim();
}

function normalizeHandle(value) {
  return cleanText(value).replace(/^@+/, "").toLowerCase();
}

function coerceUid(value) {
  const text = cleanText(value);
  return /^\d+$/.test(text) ? text : "";
}

function parseUrl(value) {
  const text = cleanText(value);
  if (!text) return null;
  try {
    return new URL(text);
  } catch {
    return null;
  }
}

function urlPlatform(value) {
  const parsed = parseUrl(value);
  if (!parsed) return "";
  const host = parsed.hostname.toLowerCase();
  if (host.endsWith("makerworld.com.cn")) return "cn";
  if (host.endsWith("makerworld.com")) return "global";
  return "";
}

function normalizedSourceUrl(value) {
  const parsed = parseUrl(value);
  if (!parsed) return "";
  const pathname = parsed.pathname.replace(/\/+$/, "").toLowerCase();
  return `${parsed.hostname.toLowerCase()}${pathname}`;
}

function isDefaultFavoritesUrl(value) {
  const parsed = parseUrl(value);
  if (!parsed) return false;
  const pathname = parsed.pathname.replace(/\/+$/, "").toLowerCase();
  return /\/@[^/]+\/collections\/models$/.test(pathname);
}

function authorHandleFromUrl(value) {
  const parsed = parseUrl(value);
  if (!parsed) return "";
  const match = parsed.pathname.match(/\/@([^/]+)/);
  return match ? normalizeHandle(decodeURIComponent(match[1] || "")) : "";
}

function uidFromUserHandle(handle) {
  const match = normalizeHandle(handle).match(/^user_(\d+)$/);
  return match ? match[1] : "";
}

function authorKeysFromSource(source) {
  const keys = new Set();
  if (!source || typeof source !== "object") return keys;

  const url = cleanText(source.url);
  const normalizedUrl = normalizedSourceUrl(url);
  if (normalizedUrl) keys.add(`url:${normalizedUrl}`);

  const uid = coerceUid(source.uid || source.user_id || source.userId);
  if (uid) keys.add(`uid:${uid}`);

  const explicitHandle = normalizeHandle(source.handle || source.user_name || source.username);
  if (explicitHandle) {
    keys.add(`handle:${explicitHandle}`);
    const handleUid = uidFromUserHandle(explicitHandle);
    if (handleUid) keys.add(`uid:${handleUid}`);
  }

  const urlHandle = authorHandleFromUrl(url);
  if (urlHandle) {
    keys.add(`handle:${urlHandle}`);
    const handleUid = uidFromUserHandle(urlHandle);
    if (handleUid) keys.add(`uid:${handleUid}`);
  }

  return keys;
}

function collectionIdFromUrl(value) {
  const parsed = parseUrl(value);
  if (!parsed) return "";
  const match = parsed.pathname.match(/\/collections\/(\d+)/);
  return match ? match[1] : "";
}

function collectionKeysFromSource(source) {
  const keys = new Set();
  if (!source || typeof source !== "object") return keys;

  const url = cleanText(source.url);
  const normalizedUrl = normalizedSourceUrl(url);
  if (normalizedUrl) keys.add(`url:${normalizedUrl}`);

  const id = coerceUid(source.id || source.collection_id || source.collectionId || source.favoriteId);
  if (id) keys.add(`collection:${id}`);

  const urlId = collectionIdFromUrl(url);
  if (urlId) keys.add(`collection:${urlId}`);

  return keys;
}

function subscriptionSources(subscriptions, platform, mode) {
  return (Array.isArray(subscriptions) ? subscriptions : [])
    .filter((item) => item && typeof item === "object")
    .filter((item) => !mode || item.mode === mode)
    .filter((item) => {
      const detectedPlatform = urlPlatform(item.url);
      return !platform || !detectedPlatform || detectedPlatform === platform;
    });
}

function countMatchedSources(sourceItems, subscriptions, platform, mode, keyFactory) {
  const sources = Array.isArray(sourceItems) ? sourceItems : [];
  if (!sources.length) return 0;

  const subscriptionKeys = new Set();
  subscriptionSources(subscriptions, platform, mode).forEach((subscription) => {
    keyFactory(subscription).forEach((key) => subscriptionKeys.add(key));
  });

  let count = 0;
  sources.forEach((source) => {
    const matched = [...keyFactory(source)].some((key) => subscriptionKeys.has(key));
    if (matched) count += 1;
  });
  return count;
}

function countImportedSources(inventory, sourceKind) {
  const importedSources = Array.isArray(inventory?.imported_sources)
    ? inventory.imported_sources
    : [];
  const urls = new Set();
  importedSources.forEach((source) => {
    if (!source || typeof source !== "object" || source.source_kind !== sourceKind) {
      return;
    }
    const url = cleanText(source.url);
    if (url) urls.add(url);
  });
  return urls.size;
}

function countPlatformSubscriptions(subscriptions, platform, mode) {
  const urls = new Set();
  subscriptionSources(subscriptions, platform, mode).forEach((subscription) => {
    const url = normalizedSourceUrl(subscription.url);
    if (url) urls.add(url);
  });
  return urls.size;
}

function countDefaultFavoriteSubscriptions(subscriptions, platform) {
  const urls = new Set();
  subscriptionSources(subscriptions, platform, "collection_models").forEach((subscription) => {
    if (!isDefaultFavoritesUrl(subscription.url)) return;
    const url = normalizedSourceUrl(subscription.url);
    if (url) urls.add(url);
  });
  return urls.size;
}

export function accountSyncedSourceCounts(inventory, subscriptions, platform = "") {
  const sourceInventory = inventory && typeof inventory === "object" ? inventory : {};
  const defaultFavorites = sourceInventory.default_favorites && typeof sourceInventory.default_favorites === "object"
    ? sourceInventory.default_favorites
    : {};
  const followedAuthors = Array.isArray(sourceInventory.followed_authors)
    ? sourceInventory.followed_authors
    : [];
  const followedCollections = Array.isArray(sourceInventory.followed_collections)
    ? sourceInventory.followed_collections
    : [];

  return {
    defaultFavorites: defaultFavorites.url
      ? countMatchedSources([defaultFavorites], subscriptions, platform, "collection_models", collectionKeysFromSource)
      : Math.min(countDefaultFavoriteSubscriptions(subscriptions, platform), 1),
    followedAuthors: followedAuthors.length
      ? countMatchedSources(followedAuthors, subscriptions, platform, "author_upload", authorKeysFromSource)
      : countImportedSources(sourceInventory, "followed_author")
        || countPlatformSubscriptions(subscriptions, platform, "author_upload"),
    followedCollections: followedCollections.length
      ? countMatchedSources(followedCollections, subscriptions, platform, "collection_models", collectionKeysFromSource)
      : countImportedSources(sourceInventory, "followed_collection"),
  };
}
