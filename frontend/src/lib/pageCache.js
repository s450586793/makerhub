const PAGE_CACHE_TTL_MS = 10 * 60 * 1000;
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
  return cloneValue(entry.value);
}


export function setPageCache(key, value) {
  const normalizedKey = String(key || "").trim();
  if (!normalizedKey || value === undefined) {
    return;
  }
  pageCache.set(normalizedKey, {
    value: cloneValue(value),
    updatedAt: Date.now(),
  });
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
