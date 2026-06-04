export function createPageRefreshScheduler({
  refresh,
  delayMs = 250,
  hiddenResumeReason = "visibility-resumed",
  isHidden = () => false,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
} = {}) {
  let timer = 0;
  let disposed = false;
  let inFlight = false;
  let pendingReason = "";
  let pendingWhenVisible = false;

  function clearTimer() {
    if (timer) {
      clearTimeoutFn(timer);
      timer = 0;
    }
  }

  async function run(reason) {
    if (disposed || typeof refresh !== "function") {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      pendingReason = reason || pendingReason;
      return;
    }
    if (inFlight) {
      pendingReason = reason || pendingReason;
      return;
    }
    inFlight = true;
    try {
      await refresh(reason);
    } finally {
      inFlight = false;
      const nextReason = pendingReason;
      pendingReason = "";
      if (nextReason && !disposed) {
        schedule(nextReason);
      }
    }
  }

  function schedule(reason = "scheduled") {
    if (disposed) {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      pendingReason = reason || pendingReason;
      clearTimer();
      return;
    }
    if (inFlight) {
      pendingReason = reason || pendingReason;
      clearTimer();
      return;
    }
    pendingReason = reason;
    clearTimer();
    timer = setTimeoutFn(() => {
      timer = 0;
      const nextReason = pendingReason || reason;
      pendingReason = "";
      void run(nextReason);
    }, Math.max(Number(delayMs) || 0, 0));
  }

  function handleVisible() {
    if (disposed || isHidden() || !pendingWhenVisible) {
      return;
    }
    pendingWhenVisible = false;
    schedule(hiddenResumeReason);
  }

  function dispose() {
    disposed = true;
    pendingReason = "";
    pendingWhenVisible = false;
    clearTimer();
  }

  return {
    clear: clearTimer,
    dispose,
    handleVisible,
    schedule,
  };
}
