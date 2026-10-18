// Light and dark themes: the system's choice until the user picks one (remembered in
// localStorage, which holds preferences only, never credentials).

import { useCallback, useEffect, useState } from "react";

export type ThemeName = "light" | "dark";
export interface Theme {
  name: ThemeName;
  toggle: () => void;
}

const key = "hardcharger-ui:theme";

function initial(): ThemeName {
  try {
    const saved = window.localStorage.getItem(key);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // no storage
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useTheme(): Theme {
  const [name, setName] = useState<ThemeName>(initial);
  useEffect(() => {
    document.documentElement.dataset["theme"] = name;
  }, [name]);
  const toggle = useCallback(() => {
    setName((current) => {
      const next = current === "light" ? "dark" : "light";
      try {
        window.localStorage.setItem(key, next);
      } catch {
        // not remembered
      }
      return next;
    });
  }, []);
  return { name, toggle };
}
