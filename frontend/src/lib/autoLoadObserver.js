export function loadMoreTriggerElement(triggerRef) {
  const value = triggerRef?.value;
  const candidates = Array.isArray(value) ? value : [value];
  return candidates.find((element) => (
    element && typeof element.getBoundingClientRect === "function"
  )) || null;
}

function defaultWindow() {
  return typeof window !== "undefined" ? window : null;
}

async function defaultNextFrame(win) {
  if (!win || typeof win.requestAnimationFrame !== "function") {
    return;
  }
  await new Promise((resolve) => {
    win.requestAnimationFrame(() => resolve());
  });
}

export function createAutoLoadObserver({
  triggerRef,
  canLoad,
  isLoading,
  load,
  nextTick,
  rootMargin = "0px 0px 420px 0px",
  nearViewportMargin = 420,
  win = defaultWindow,
  waitForNextFrame = defaultNextFrame,
}) {
  let intersectionObserver = null;
  let observerToken = 0;

  function currentWindow() {
    return typeof win === "function" ? win() : win;
  }

  function disconnect() {
    observerToken += 1;
    if (intersectionObserver) {
      intersectionObserver.disconnect();
      intersectionObserver = null;
    }
  }

  function isTriggerNearViewport(margin = nearViewportMargin) {
    const currentWin = currentWindow();
    const element = loadMoreTriggerElement(triggerRef);
    if (!currentWin || !element) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.top <= currentWin.innerHeight + margin && rect.bottom >= -margin;
  }

  async function loadIfTriggerIsVisible(currentObserverToken) {
    if (typeof nextTick === "function") {
      await nextTick();
    }
    await waitForNextFrame(currentWindow());
    if (
      currentObserverToken !== observerToken
      || (typeof isLoading === "function" && isLoading())
      || (typeof canLoad === "function" && !canLoad())
      || !isTriggerNearViewport()
    ) {
      return;
    }
    void load();
  }

  function ensure() {
    disconnect();
    const currentWin = currentWindow();
    const Observer = currentWin?.IntersectionObserver;
    const element = loadMoreTriggerElement(triggerRef);
    if (
      !currentWin
      || typeof Observer !== "function"
      || !element
      || (typeof isLoading === "function" && isLoading())
      || (typeof canLoad === "function" && !canLoad())
    ) {
      return;
    }
    const currentObserverToken = ++observerToken;
    intersectionObserver = new Observer((entries) => {
      const [entry] = entries;
      if (entry?.isIntersecting) {
        void load();
      }
    }, {
      rootMargin,
    });
    intersectionObserver.observe(element);
    void loadIfTriggerIsVisible(currentObserverToken);
  }

  return {
    disconnect,
    ensure,
    isTriggerNearViewport,
  };
}
