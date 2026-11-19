import { useCallback, useEffect, useEffectEvent, useState } from "react";
import { ApiError } from "./api";

/** Calls fn at once, then every ms after the last answer came back. Nothing polls while the tab is hidden; showing it
 * again polls at once (the plan's Display rules). A failed poll keeps the last good data beside its error. refresh()
 * polls now. fn is read fresh on every poll: a page that starts asking for something else (another id) remounts. */
export function usePoll<T>(fn: () => Promise<T>, ms: number) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError>();
  const [round, setRound] = useState(0);
  const load = useEffectEvent(fn);

  useEffect(() => {
    let live = true;
    let busy = false;
    let timer = 0;
    const poll = async () => {
      window.clearTimeout(timer);
      if (busy || document.hidden) return; // the poll in flight sets the next timer; a hidden tab waits to be shown
      busy = true;
      try {
        const got = await load();
        if (live) {
          setData(got);
          setError(undefined);
        }
      } catch (e) {
        if (live) setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
      }
      busy = false;
      if (live) timer = window.setTimeout(poll, ms);
    };
    const wake = () => void poll();
    document.addEventListener("visibilitychange", wake);
    void poll();
    return () => {
      live = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [ms, round]);

  const refresh = useCallback(() => setRound((r) => r + 1), []);
  return { data, error, refresh };
}
