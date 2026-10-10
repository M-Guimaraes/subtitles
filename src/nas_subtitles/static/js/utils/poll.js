// Shared polling loop (plan §18.2): never overlaps itself, pauses while the
// tab is hidden, and returns a `stop()` the caller must call on navigation.

export function startPolling(task, intervalMs) {
  let stopped = false;
  let running = false;
  const tick = async () => {
    if (stopped || running || document.hidden) return;
    running = true;
    try {
      await task();
    } finally {
      running = false;
    }
  };
  const timer = setInterval(tick, intervalMs);
  const onVisible = () => {
    if (!document.hidden) tick();
  };
  document.addEventListener("visibilitychange", onVisible);
  return () => {
    stopped = true;
    clearInterval(timer);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
