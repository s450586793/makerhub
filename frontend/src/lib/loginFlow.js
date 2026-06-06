import { apiRequest } from "./api.js";
import { applyLoginSession, refreshBootstrap } from "./appState.js";
import { safeNextPath } from "./helpers.js";


export async function submitLogin({
  username,
  password,
  next = "/",
  request = apiRequest,
  applySession = applyLoginSession,
  refresh = refreshBootstrap,
  router,
} = {}) {
  const payload = await request("/api/auth/login", {
    method: "POST",
    body: {
      username,
      password,
    },
    redirectOn401: false,
  });
  applySession(payload);
  const target = safeNextPath(next || "/");
  if (!router || typeof router.replace !== "function") {
    throw new Error("登录导航器未初始化。");
  }
  await router.replace(target);
  if (typeof refresh === "function") {
    refresh().catch((error) => {
      console.error("登录后状态刷新失败", error);
    });
  }
  return payload;
}
