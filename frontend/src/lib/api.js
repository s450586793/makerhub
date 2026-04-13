function buildRedirectTarget() {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}


export async function apiRequest(path, options = {}) {
  const {
    method = "GET",
    body = undefined,
    headers = {},
    redirectOn401 = true,
  } = options;

  const requestHeaders = new Headers(headers);
  let requestBody = body;

  if (body !== undefined && body !== null && !(body instanceof FormData) && typeof body !== "string") {
    requestHeaders.set("Content-Type", "application/json");
    requestBody = JSON.stringify(body);
  }

  if (!requestHeaders.has("Accept")) {
    requestHeaders.set("Accept", "application/json");
  }

  const response = await fetch(path, {
    method,
    headers: requestHeaders,
    body: requestBody,
    credentials: "include",
  });

  if (response.status === 401 && redirectOn401) {
    const next = encodeURIComponent(buildRedirectTarget());
    window.location.assign(`/login?next=${next}`);
    throw new Error("未登录。");
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("Content-Type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null
      ? payload.detail || payload.message
      : payload;
    throw new Error(String(detail || "请求失败。"));
  }

  return payload;
}
