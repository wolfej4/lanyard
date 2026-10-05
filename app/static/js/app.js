// Small progressive enhancements. Everything works without this file.
document.addEventListener("DOMContentLoaded", () => {
  // Countdown for an unpaid checkout hold.
  const hold = document.querySelector("[data-hold-until]");
  if (hold) {
    const until = Date.parse(hold.dataset.holdUntil);
    const tick = () => {
      const s = Math.max(0, Math.round((until - Date.now()) / 1000));
      hold.textContent = s > 0 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "expired";
      if (s > 0) setTimeout(tick, 1000);
    };
    tick();
  }
  // Confirm buttons: <button data-confirm="Are you sure?">
  document.body.addEventListener("click", (e) => {
    const b = e.target.closest("[data-confirm]");
    if (b && !window.confirm(b.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
});
