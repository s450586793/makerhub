import { onActivated, onBeforeUnmount, onDeactivated, ref } from "vue";

export function useKeepAlivePage({ onActivate, onDeactivate } = {}) {
  const active = ref(false);
  let activated = false;
  let cleanedUp = false;
  let activationRevision = 0;

  async function activate() {
    cleanedUp = false;
    active.value = true;
    const revision = ++activationRevision;
    const initial = !activated;
    activated = true;
    await onActivate?.({
      initial,
      isCurrent: () => active.value && activationRevision === revision,
    });
  }

  function deactivate() {
    if (cleanedUp) {
      return;
    }
    cleanedUp = true;
    active.value = false;
    activationRevision += 1;
    onDeactivate?.();
  }

  onActivated(() => {
    void activate();
  });
  onDeactivated(deactivate);
  onBeforeUnmount(deactivate);

  return { active };
}
