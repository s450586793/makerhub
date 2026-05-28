const MODEL_RETURN_STATE_PREFIX = "model-return:";
const VALID_RETURN_CONTEXTS = new Set(["subscriptions", "organizer"]);


function normalizeDetailPath(value) {
  const raw = String(value || "").trim();
  if (!raw.startsWith("/models/") || raw.startsWith("//")) {
    return "";
  }
  const cleanPath = raw.split("#", 1)[0].split("?", 1)[0];
  return /^\/models\/(?:mwcn|mwg|local)\d+$/.test(cleanPath) ? cleanPath : "";
}


function normalizeInternalReturnPath(value) {
  const raw = String(value || "").trim();
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) {
    return "";
  }
  return raw;
}


export function normalizeModelReturnContext(value) {
  const raw = Array.isArray(value) ? value[0] : value;
  const normalized = String(raw || "").trim();
  return VALID_RETURN_CONTEXTS.has(normalized) ? normalized : "";
}


export function inferModelReturnContext(value) {
  const raw = normalizeInternalReturnPath(Array.isArray(value) ? value[0] : value);
  if (!raw) {
    return "";
  }
  try {
    const url = new URL(raw, "http://makerhub.local");
    const explicit = normalizeModelReturnContext(url.searchParams.get("nav_context"));
    if (explicit) {
      return explicit;
    }
    if (url.pathname.startsWith("/models/state/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/local/")) {
      return "organizer";
    }
    if (url.pathname.startsWith("/models/source/")) {
      return "subscriptions";
    }
  } catch {
    return "";
  }
  return "";
}


export function buildModelDetailRoute(detailPath, returnState = {}) {
  const path = normalizeDetailPath(detailPath);
  if (!path) {
    return null;
  }
  return { path };
}


export function modelReturnStateKey(detailPath) {
  const path = normalizeDetailPath(detailPath);
  return path ? `${MODEL_RETURN_STATE_PREFIX}${path}` : "";
}


export function storeModelReturnState(storage, detailPath, returnState = {}) {
  const key = modelReturnStateKey(detailPath);
  if (!key || !storage || typeof storage.setItem !== "function") {
    return false;
  }
  const returnTo = normalizeInternalReturnPath(returnState.returnTo);
  const returnContext = normalizeModelReturnContext(returnState.returnContext)
    || inferModelReturnContext(returnTo);
  if (!returnTo && !returnContext) {
    return false;
  }
  try {
    storage.setItem(
      key,
      JSON.stringify({
        returnTo,
        returnContext,
      }),
    );
    return true;
  } catch {
    return false;
  }
}


export function getStoredModelReturnState(storage, detailPath) {
  const key = modelReturnStateKey(detailPath);
  if (!key || !storage || typeof storage.getItem !== "function") {
    return {};
  }
  try {
    const payload = JSON.parse(storage.getItem(key) || "{}");
    if (!payload || typeof payload !== "object") {
      return {};
    }
    const returnTo = normalizeInternalReturnPath(payload.returnTo);
    const returnContext = normalizeModelReturnContext(payload.returnContext)
      || inferModelReturnContext(returnTo);
    if (!returnTo && !returnContext) {
      return {};
    }
    return {
      returnTo,
      returnContext,
    };
  } catch {
    return {};
  }
}


export function storeModelReturnStateFromRoute(storage, detailPath, query = {}) {
  const returnTo = Array.isArray(query.return_to) ? query.return_to[0] : query.return_to;
  const returnContext = Array.isArray(query.return_context) ? query.return_context[0] : query.return_context;
  return storeModelReturnState(storage, detailPath, {
    returnTo,
    returnContext,
  });
}
