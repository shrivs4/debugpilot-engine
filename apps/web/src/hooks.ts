import { useCallback, useEffect, useRef, useState } from "react";

/** Load data once, then re-poll every `intervalMs` while `shouldPoll(data)` is true. */
export function usePolling<T>(load: () => Promise<T>, shouldPoll: (data: T) => boolean, intervalMs = 1500) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  const pollRef = useRef(shouldPoll);
  loadRef.current = load;
  pollRef.current = shouldPoll;

  const refresh = useCallback(async () => {
    try {
      const next = await loadRef.current();
      setData(next);
      setError(null);
      return next;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const next = await refresh();
      if (!cancelled && next !== null && pollRef.current(next)) {
        timer = setTimeout(tick, intervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [refresh, intervalMs]);

  return { data, error, refresh, setData };
}

export function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash.slice(1) || "/repositories");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash.slice(1) || "/repositories");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export function navigate(path: string) {
  window.location.hash = path;
}
