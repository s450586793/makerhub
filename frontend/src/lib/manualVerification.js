export function verificationImageUrl(image = "") {
  const value = String(image || "").trim();
  return value && /^[A-Za-z0-9+/]+={0,2}$/.test(value)
    ? `data:image/jpeg;base64,${value}`
    : "";
}

export function verificationPointerPoint(event, bounds, width, height) {
  const values = [event?.clientX, event?.clientY, bounds?.left, bounds?.top, bounds?.width, bounds?.height, width, height];
  if (!values.every(Number.isFinite) || bounds.width <= 0 || bounds.height <= 0 || width <= 0 || height <= 0) return null;
  return {
    x: Math.max(0, Math.min(width - 1, (event.clientX - bounds.left) * width / bounds.width)),
    y: Math.max(0, Math.min(height - 1, (event.clientY - bounds.top) * height / bounds.height)),
  };
}

const REMOTE_KEYS = new Set(["Enter", "Tab", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

export function verificationKeyboardInput(event = {}) {
  const key = String(event.key || "");
  if (event.isComposing || !key || ["Dead", "Process", "Unidentified"].includes(key)) return null;
  const modifiers = [
    event.ctrlKey && "Control", event.metaKey && "Meta", event.altKey && "Alt", event.shiftKey && "Shift",
  ].filter(Boolean);
  if (!event.ctrlKey && !event.metaKey && !event.altKey && Array.from(key).length === 1) {
    return { type: "text", text: key };
  }
  if (!REMOTE_KEYS.has(key) && !(Array.from(key).length === 1 && modifiers.length)) return null;
  return { type: "key", key: [...modifiers, key].join("+") };
}

export function createManualVerificationController({ request, onChange = () => {}, onComplete = () => {}, pollInterval = 900, schedule = setTimeout, unschedule = clearTimeout } = {}) {
  let current = null;
  let cleanup = Promise.resolve();

  function active(session) {
    return current === session && !session.closed;
  }

  function publish(session, patch) {
    if (!active(session)) return;
    session.state = { ...session.state, ...patch };
    onChange(session.state);
  }

  function clearPoll(session) {
    if (session.timer !== null) unschedule(session.timer);
    session.timer = null;
  }

  function terminal(session) {
    return session.state.completed || ["error", "failed", "cancelled", "expired"].includes(session.state.status);
  }

  function poll(session) {
    clearPoll(session);
    if (!active(session) || !session.id || terminal(session) || session.state.finishing || session.inputPromise) return;
    session.timer = schedule(() => {
      session.timer = null;
      void refresh(session);
    }, pollInterval);
  }

  function apply(session, payload = {}) {
    if (!active(session)) return;
    if (payload.session_id !== session.id || payload.platform !== session.platform) {
      publish(session, { status: "error" });
      throw new Error("验证会话不匹配，请关闭后重试。");
    }
    publish(session, {
      ...payload,
      loading: !(payload.image || session.state.image || payload.completed === true || ["error", "failed", "cancelled", "expired"].includes(payload.status)),
      error: "",
      image: payload.image || session.state.image,
      width: payload.width || session.state.width,
      height: payload.height || session.state.height,
      completed: payload.completed === true,
    });
    if (session.state.completed && !session.notified) {
      session.notified = true;
      clearPoll(session);
      onComplete(payload);
    }
  }

  function report(session, error) {
    publish(session, { loading: false, error: error instanceof Error ? error.message : "验证窗口暂时不可用，请重试。" });
  }

  function enqueue(session, operation) {
    const pending = session.queue.catch(() => {}).then(async () => {
      if (!active(session) || terminal(session)) return;
      return operation();
    });
    session.queue = pending;
    return pending;
  }

  function discard(session, id = session.id) {
    if (!id) return Promise.resolve();
    if (!session.discardPromise) {
      session.discardPromise = Promise.resolve().then(() => request(`${session.endpoint}/${encodeURIComponent(id)}`, { method: "DELETE" })).catch(() => {
        // 服务端会按会话有效期回收，关闭窗口不因网络故障被阻塞。
      });
    }
    return session.discardPromise;
  }

  function close() {
    const session = current;
    if (!session) return cleanup;
    current = null;
    session.closed = true;
    session.buffer.length = 0;
    clearPoll(session);
    cleanup = cleanup.then(async () => {
      if (!session.id && session.createPromise) {
        const payload = await session.createPromise.catch(() => null);
        session.id = String(payload?.session_id || "");
      }
      await discard(session);
    });
    return cleanup;
  }

  async function open(action = {}) {
    const closing = close();
    const endpoint = String(action.endpoint || "").replace(/\/$/, "");
    const match = endpoint.match(/^\/api\/config\/online-accounts\/(cn|global)\/verification$/);
    const session = {
      endpoint, platform: match?.[1] || "", id: "", closed: false, queue: Promise.resolve(),
      timer: null, framePromise: null, inputPromise: null, createPromise: null, discardPromise: null, buffer: [], notified: false,
      state: { loading: true, finishing: false, status: "loading", message: "正在打开验证页面。", error: "", image: "", width: 0, height: 0, completed: false },
    };
    current = session;
    publish(session, {});
    if (!match) {
      publish(session, { loading: false, status: "error", error: "验证入口无效，请刷新首页后重试。" });
      return;
    }
    try {
      await closing;
      if (!active(session)) return;
      session.createPromise = request(endpoint, { method: "POST", body: action.body || {} });
      const payload = await session.createPromise;
      session.id = String(payload?.session_id || "");
      if (!active(session)) {
        await discard(session);
        return;
      }
      if (!session.id) throw new Error("未能创建验证会话，请重试。");
      apply(session, payload);
      if (!session.state.image && !terminal(session)) await refresh(session);
      else poll(session);
    } catch (error) {
      report(session, error);
      publish(session, { status: "error" });
    }
  }

  function refresh(session = current) {
    if (!session || !active(session) || !session.id || terminal(session)) return Promise.resolve();
    if (session.framePromise) return session.framePromise;
    clearPoll(session);
    session.framePromise = enqueue(session, async () => {
      const payload = await request(`${session.endpoint}/${encodeURIComponent(session.id)}/frame`, { method: "POST" });
      apply(session, payload);
    }).catch((error) => report(session, error)).finally(() => {
      session.framePromise = null;
      poll(session);
    });
    return session.framePromise;
  }

  function sendEvents(events = []) {
    const session = current;
    if (!session || !session.id || !active(session) || terminal(session) || session.state.finishing) return Promise.resolve();
    clearPoll(session);
    for (const event of events) {
      if (session.buffer.length >= 600) {
        const move = session.buffer.findIndex((pending) => pending.type === "pointermove");
        if (move !== -1) session.buffer.splice(move, 1);
        else if (event.type !== "pointerup") {
          report(session, new Error("操作积压，请等待当前操作完成后重试。"));
          continue;
        }
      }
      session.buffer.push(event);
    }
    if (session.inputPromise) return session.inputPromise;
    session.inputPromise = enqueue(session, async () => {
      while (active(session) && session.buffer.length && !terminal(session)) {
        const batch = session.buffer.splice(0, 80);
        const payload = await request(`${session.endpoint}/${encodeURIComponent(session.id)}/input`, { method: "POST", body: { events: batch } });
        apply(session, payload);
      }
    }).catch((error) => {
      session.buffer.length = 0;
      report(session, error);
    }).finally(() => {
      session.inputPromise = null;
      if (active(session)) void refresh(session);
    });
    return session.inputPromise;
  }

  async function finish() {
    const session = current;
    if (!session || !session.id || !active(session) || terminal(session) || session.state.finishing) return;
    clearPoll(session);
    publish(session, { finishing: true, error: "" });
    try {
      await enqueue(session, async () => {
        const payload = await request(`${session.endpoint}/${encodeURIComponent(session.id)}/finish`, { method: "POST" });
        apply(session, payload);
      });
    } catch (error) {
      report(session, error);
    } finally {
      publish(session, { finishing: false });
      poll(session);
    }
  }

  return { open, close, refresh: () => refresh(), sendEvents, finish };
}
