function isAbortError(error) {
  return error?.name === "AbortError";
}

export function createHydratedResource({
  load: loadResource,
  enrich: enrichResource,
  merge = (_current, incoming) => incoming,
  cache,
  onData,
  onError,
  onLoading,
} = {}) {
  let currentRevision = 0;
  let controller = null;
  let currentValue;

  if (typeof cache?.get === "function") {
    currentValue = cache.get();
  }

  function state(phase, revision) {
    return { phase, revision, signal: controller?.signal };
  }

  async function run(loader, phase, options = {}) {
    if (typeof loader !== "function") {
      return currentValue;
    }
    controller?.abort();
    controller = new AbortController();
    const revision = ++currentRevision;
    const runController = controller;
    onLoading?.(true, state(phase, revision));
    try {
      const incoming = await loader({
        ...(options || {}),
        current: currentValue,
        phase,
        revision,
        signal: runController.signal,
      });
      if (revision !== currentRevision || runController.signal.aborted) {
        return undefined;
      }
      currentValue = phase === "enrich"
        ? merge(currentValue, incoming, state(phase, revision))
        : incoming;
      cache?.set?.(currentValue);
      onData?.(currentValue, state(phase, revision));
      return currentValue;
    } catch (error) {
      if (revision !== currentRevision || runController.signal.aborted || isAbortError(error)) {
        return undefined;
      }
      onError?.(error, state(phase, revision));
      throw error;
    } finally {
      if (revision === currentRevision) {
        controller = null;
        onLoading?.(false, { phase, revision, signal: runController.signal });
      }
    }
  }

  return {
    load: (options) => run(loadResource, "load", options),
    enrich: (options) => run(
      typeof enrichResource === "function"
        ? (context) => enrichResource(currentValue, context)
        : null,
      "enrich",
      options,
    ),
    cancel: () => {
      const previousController = controller;
      currentRevision += 1;
      controller = null;
      previousController?.abort();
      if (previousController) {
        onLoading?.(false, { phase: "cancel", revision: currentRevision, signal: previousController.signal });
      }
    },
    get revision() {
      return currentRevision;
    },
    get value() {
      return currentValue;
    },
  };
}
