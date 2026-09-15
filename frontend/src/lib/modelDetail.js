export function profileMachines(profile) {
  const raw = profile?.machines || profile?.machine || [];
  const values = Array.isArray(raw) ? raw : String(raw).split(/[,，、;；/|]+/);
  return [...new Set(values.map(item => String(typeof item === "object" ? item?.name || "" : item).trim()).filter(Boolean))];
}

export function availableMachines(profiles = []) {
  return [...new Set(profiles.flatMap(profileMachines).filter(name => name !== "通用"))];
}

export function filterProfiles(profiles = [], machine = "") {
  if (!machine) return profiles;
  const key = machine.trim().toLowerCase();
  return profiles.filter(profile => {
    const names = profileMachines(profile).map(name => name.toLowerCase());
    return !names.length || names.includes("通用") || names.includes(key);
  });
}

export function nextGalleryIndex(current, offset, length) {
  if (!Number.isInteger(length) || length <= 0) return -1;
  const index = Number.isInteger(current) && current >= 0 && current < length ? current : 0;
  return ((index + offset) % length + length) % length;
}

export function profilePopoverPosition(rect, viewport) {
  const margin = 16;
  const width = Math.min(480, viewport.width - margin * 2);
  const leftFits = rect.left >= width + margin + 12;
  return {
    width,
    left: leftFits ? rect.left - width - 12 : Math.max(margin, Math.min(rect.left, viewport.width - width - margin)),
    top: Math.max(margin, Math.min(leftFits ? rect.top : rect.bottom + 8, viewport.height - Math.min(560, viewport.height - margin * 2) - margin)),
    maxHeight: Math.max(120, viewport.height - margin * 2),
    placement: leftFits ? "left" : "below",
  };
}

export function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["https:", "http:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}
