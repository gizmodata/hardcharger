// A size the user drags (the editor's height, the side panel's width), remembered in this
// browser and shared by every editor tab. Storage may be missing or blocked: the size then
// lasts only until the page reloads.

import { useCallback, useEffect, useState } from "react";

export interface SizeSpec {
  key: string;
  defaultPx: number;
  minPx: number;
  /** The largest size now (it follows the window). */
  maxPx: () => number;
}

const clamp = (v: number, spec: SizeSpec) => Math.round(Math.max(spec.minPx, Math.min(spec.maxPx(), v)));

function read(spec: SizeSpec): number {
  try {
    const n = Number(window.localStorage.getItem(spec.key));
    if (Number.isFinite(n) && n > 0) return clamp(n, spec);
  } catch {
    // no storage
  }
  return clamp(spec.defaultPx, spec);
}

export function useStoredSize(spec: SizeSpec): [number, (px: number) => void, () => void] {
  const [size, setSize] = useState(() => read(spec));
  const { key, defaultPx } = spec;
  const set = useCallback(
    (px: number) => {
      const next = clamp(px, spec);
      setSize(next);
      try {
        window.localStorage.setItem(key, String(next));
      } catch {
        // not remembered
      }
    },
    // The spec's numbers are constants of the caller: the key names them.
    [key],
  );
  const reset = useCallback(() => {
    setSize(clamp(defaultPx, spec));
    try {
      window.localStorage.removeItem(key);
    } catch {
      // nothing to forget
    }
  }, [key, defaultPx]);
  // Another tab of the editor (or another browser tab) dragged it: follow.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === key) setSize(read(spec));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [key]);
  return [size, set, reset];
}
