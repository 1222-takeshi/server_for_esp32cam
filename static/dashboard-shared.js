// Shared client-side helpers for the live dashboard pages.
//
// templates/index.html and templates/camera_detail.html each defined an
// identical formatTimestamp()/formatFrameSize() pair and identical
// RECONNECT_MS/STALE_AFTER_MS/OFFLINE_AFTER_MS constants inline (see #358).
// Factored out here, loaded once from _base.html, so both pages share one
// cacheable definition instead of duplicating it verbatim. The differing
// per-page reconnect/status scaffolding (grid vs. single-camera DOM
// updates) is intentionally left in each template's own script block.
window.CameraShared = (function () {
  function formatTimestamp(value) {
    if (!value) return null;
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return null;
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Tokyo',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }).formatToParts(d).reduce((acc, part) => {
      acc[part.type] = part.value;
      return acc;
    }, {});
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} JST`;
  }

  function formatFrameSize(bytes) {
    const size = Number(bytes);
    if (!Number.isFinite(size) || size < 0) return null;
    if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024).toFixed(1)} KB`;
  }

  return {
    RECONNECT_MS: 3000,
    STALE_AFTER_MS: 15000,
    OFFLINE_AFTER_MS: 60000,
    formatTimestamp,
    formatFrameSize,
  };
})();
