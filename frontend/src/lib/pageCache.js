const PAGE_CACHE_TTL_MS = 10 * 60 * 1000;
const PAGE_CACHE_MAX_ITEMS = 32;
const pageCache = new Map();


function cloneValue(value) {
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(value);
    } catch {
      // Vue reactive proxies cannot always be structured-cloned directly.
    }
  }
  return JSON.parse(JSON.stringify(value));
}


export function getPageCache(key) {
  const normalizedKey = String(key || "").trim();
  if (!normalizedKey) {
    return null;
  }
  const entry = pageCache.get(normalizedKey);
  if (!entry) {
    return null;
  }
  if (Date.now() - Number(entry.updatedAt || 0) > PAGE_CACHE_TTL_MS) {
    pageCache.delete(normalizedKey);
    return null;
  }
  pageCache.delete(normalizedKey);
  pageCache.set(normalizedKey, entry);
  return cloneValue(entry.value);
}


export function setPageCache(key, value) {
  const normalizedKey = String(key || "").trim();
  if (!normalizedKey || value === undefined) {
    return;
  }
  pageCache.delete(normalizedKey);
  pageCache.set(normalizedKey, {
    value: cloneValue(value),
    updatedAt: Date.now(),
  });
  for (const [cacheKey, entry] of pageCache) {
    if (Date.now() - Number(entry.updatedAt || 0) > PAGE_CACHE_TTL_MS || pageCache.size > PAGE_CACHE_MAX_ITEMS) {
      pageCache.delete(cacheKey);
    }
  }
}


export function deletePageCache(key) {
  const normalizedKey = String(key || "").trim();
  if (!normalizedKey) {
    return;
  }
  pageCache.delete(normalizedKey);
}


export function deletePageCacheByPrefix(prefix) {
  const normalizedPrefix = String(prefix || "").trim();
  if (!normalizedPrefix) {
    return;
  }
  for (const key of pageCache.keys()) {
    if (key.startsWith(normalizedPrefix)) {
      pageCache.delete(key);
    }
  }
}


export function resetPageCacheForTests() {
  pageCache.clear();
}
