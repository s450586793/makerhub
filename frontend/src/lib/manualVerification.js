const WAITING_STATUSES = new Set(["preparing", "verifying"]);
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "expired"]);

export function createManualVerificationController({ request, onChange = () => {}, onComplete = () => {}, pollInterval = 1000, requestTimeout = 15000, schedule = setTimeout, unschedule = clearTimeout } = {}) {
  let current = null;
  let cleanup = Promise.resolve();
  const deadline = Number.isFinite(requestTimeout) && requestTimeout > 0 ? requestTimeout : 15000;

  function active(session) { return current === session && !session.closed; }

  function publish(session, patch) {
    if (!active(session)) return;
    session.state = { ...session.state, ...patch };
    onChange(session.state);
  }

  function clearPoll(session) {
    if (session.timer !== null) unschedule(session.timer);
    session.timer = null;
  }

  function terminal(session) { return session.state.completed || TERMINAL_STATUSES.has(session.state.status); }

  function poll(session) {
    clearPoll(session);
    if (!active(session) || !session.id || terminal(session) || !WAITING_STATUSES.has(session.state.status) || session.submitPromise) return;
    session.timer = schedule(() => {
      session.timer = null;
      void refresh(session);
    }, pollInterval);
  }

  function apply(session, payload = {}) {
    if (!active(session)) return;
    if (payload.session_id !== session.id || payload.platform !== session.platform) {
      throw new Error("验证会话不匹配，请关闭后重试。");
    }
    if (![...WAITING_STATUSES, ...TERMINAL_STATUSES, "challenge"].includes(payload.status)) {
      throw new Error("验证状态无效，请重新打开验证。");
    }
    publish(session, {
      ...payload,
      loading: WAITING_STATUSES.has(payload.status),
      error: "",
      challenge: payload.status === "challenge" ? payload.challenge || null : null,
      completed: payload.completed === true,
    });
    if (session.state.completed && !session.notified) {
      session.notified = true;
      clearPoll(session);
      onComplete(payload);
    }
  }

  function report(session, error) {
    clearPoll(session);
    publish(session, { loading: false, status: "failed", challenge: null, error: error instanceof Error ? error.message : "验证窗口暂时不可用，请重试。" });
  }

  function requestWithDeadline(session, path, options, onLateResponse) {
    const abort = typeof AbortController === "function" ? new AbortController() : null;
    return new Promise((resolve, reject) => {
      let settled = false;
      let timer = null;
      function finish() {
        settled = true;
        if (timer !== null) unschedule(timer);
        session?.requests.delete(cancel);
      }
      function cancel() {
        if (settled) return;
        finish();
        abort?.abort();
        const error = new Error("验证请求已取消。");
        error.name = "AbortError";
        reject(error);
      }
      session?.requests.add(cancel);
      timer = schedule(() => {
        if (settled) return;
        finish();
        abort?.abort();
        reject(new Error("验证请求超时，请重试。"));
      }, deadline);
      Promise.resolve().then(() => {
        if (settled) return;
        return request(path, { ...options, ...(abort ? { signal: abort.signal } : {}) });
      }).then((payload) => {
        if (settled) {
          if (payload && onLateResponse) onLateResponse(payload);
          return;
        }
        finish();
        resolve(payload);
      }, (error) => {
        if (settled) return;
        finish();
        reject(error);
      }).catch(() => {
        // 迟到会话仅做尽力清理，服务端仍按有效期回收。
      });
    });
  }

  function discard(session, id = session.id) {
    if (!id) return Promise.resolve();
    // 新会话可能复用了旧请求在服务端创建的 ID，不能删除它正在使用的会话。
    if (current && current !== session && active(current) && current.endpoint === session.endpoint && (!current.id || current.id === id)) return Promise.resolve();
    if (!session.discardPromise) {
      session.discardPromise = requestWithDeadline(null, `${session.endpoint}/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {
        // 服务端按有效期回收，网络故障不阻塞关闭。
      });
      cleanup = Promise.all([cleanup, session.discardPromise]).then(() => undefined);
    }
    return session.discardPromise;
  }

  function lateCreated(session, payload) {
    session.id = String(payload?.session_id || "");
    void discard(session);
  }

  function close() {
    const session = current;
    if (!session) return cleanup;
    current = null;
    session.closed = true;
    clearPoll(session);
    for (const cancel of [...session.requests]) cancel();
    void discard(session);
    return cleanup;
  }

  async function open(action = {}) {
    const closing = close();
    const endpoint = String(action.endpoint || "").replace(/\/$/, "");
    const match = endpoint.match(/^\/api\/config\/online-accounts\/(cn|global)\/verification$/);
    const session = {
      endpoint, platform: match?.[1] || "", id: "", closed: false,
      timer: null, statusPromise: null, submitPromise: null, createPromise: null, discardPromise: null, requests: new Set(), submitted: new Set(), notified: false,
      state: { loading: true, status: "preparing", message: "正在准备验证码。", error: "", challenge: null, completed: false },
    };
    current = session;
    publish(session, {});
    if (!match) {
      report(session, new Error("验证入口无效，请刷新首页后重试。"));
      return;
    }
    try {
      await closing;
      if (!active(session)) return;
      session.createPromise = requestWithDeadline(session, endpoint, { method: "POST", body: action.body || {} }, (payload) => lateCreated(session, payload));
      const payload = await session.createPromise;
      session.id = String(payload?.session_id || "");
      if (!active(session)) {
        lateCreated(session, payload);
        return;
      }
      if (!session.id) throw new Error("未能创建验证会话，请重试。");
      apply(session, payload);
      poll(session);
    } catch (error) { report(session, error); }
  }

  function refresh(session = current) {
    if (!session || !active(session) || !session.id || terminal(session)) return Promise.resolve();
    if (session.statusPromise) return session.statusPromise;
    clearPoll(session);
    session.statusPromise = Promise.resolve().then(async () => {
      if (!active(session)) return;
      const payload = await requestWithDeadline(session, `${session.endpoint}/${encodeURIComponent(session.id)}`, { method: "GET" });
      apply(session, payload);
    }).catch((error) => report(session, error)).finally(() => {
      session.statusPromise = null;
      poll(session);
    });
    return session.statusPromise;
  }

  function complete(challengeId, proof) {
    const session = current;
    if (!session || !active(session) || !session.id || terminal(session) || session.state.status !== "challenge"
      || session.state.challenge?.challenge_id !== challengeId || session.submitted.has(challengeId)) return Promise.resolve();
    session.submitted.add(challengeId);
    clearPoll(session);
    publish(session, { loading: true, status: "verifying", challenge: null, message: "正在确认验证结果。", error: "" });
    session.submitPromise = Promise.resolve().then(async () => {
      if (!active(session)) return;
      const payload = await requestWithDeadline(session, `${session.endpoint}/${encodeURIComponent(session.id)}/complete`, {
        method: "POST", body: { challenge_id: challengeId, proof },
      });
      apply(session, payload);
    }).catch((error) => report(session, error)).finally(() => {
      session.submitPromise = null;
      poll(session);
    });
    return session.submitPromise;
  }

  return { open, close, refresh: () => refresh(), complete };
}
