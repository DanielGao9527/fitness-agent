"use strict";
window.GuestSession = (() => {
  const storageKey = "fitness-guest-tab";
  let key = "";
  try {
    // A reload continues this visit; a fresh navigation must not revive old data.
    if (performance.getEntriesByType("navigation")[0]?.type === "reload")
      key = sessionStorage.getItem(storageKey) || "";
  } catch (_) { /* Storage may be unavailable in private browser modes. */ }
  if (!/^[a-f0-9]{64}$/.test(key)) key = "";
  function clear() {
    key = "";
    try { sessionStorage.removeItem(storageKey); } catch (_) { /* Memory-only visit. */ }
  }
  function start() {
    key = [...crypto.getRandomValues(new Uint8Array(32))].map(byte => byte.toString(16).padStart(2, "0")).join("");
    try { sessionStorage.setItem(storageKey, key); } catch (_) { /* Refresh will require a new visit. */ }
    return key;
  }
  return { start, clear, headers: () => key ? { "X-Fitness-Guest": key } : {} };
})();
window.addEventListener("pageshow", event => {
  if (event.persisted) window.location.reload();
});
