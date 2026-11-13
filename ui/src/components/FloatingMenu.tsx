import { useEffect, useLayoutEffect, useRef } from "react";

export interface MenuItem {
  label: string;
  /** A group's heading: shown, not a choice. */
  heading?: boolean;
  run?: () => void;
  /** Why it can't be used now (shown as its tooltip), or undefined. */
  disabled?: string | undefined;
}

/**
 * A menu at a point (a right-click, or under a … button): Esc, a click outside or picking an
 * item closes it; the first item takes the focus, and the arrow keys move between items.
 */
export function FloatingMenu({
  x,
  y,
  items,
  onClose,
  align = "left",
}: {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
  /** "right": the menu's right edge at x (under a button at the right of a toolbar). */
  align?: "left" | "right";
}) {
  const menu = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const place = () => {
      const element = menu.current;
      if (!element) return;
      // Mobile WebViews can include overflowing toolbars in innerWidth.
      // Position against the actual viewport and the measured menu instead.
      const width = document.documentElement.clientWidth;
      const height = document.documentElement.clientHeight;
      element.style.left = `${Math.max(8, Math.min(align === "right" ? x - element.offsetWidth : x, width - element.offsetWidth - 8))}px`;
      element.style.top = `${Math.max(8, Math.min(y, height - element.offsetHeight - 8))}px`;
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [x, y, align, items.length]);
  useEffect(() => {
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    menu.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      before?.focus({ preventScroll: true });
    };
  }, [onClose]);
  const move = (step: number) => {
    const buttons = [...(menu.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    buttons[(at + step + buttons.length) % buttons.length]?.focus();
  };
  return (
    <>
      <div
        className="menu-backdrop"
        onClick={onClose}
        onContextMenu={(e) => {
          e.preventDefault();
          onClose();
        }}
      />
      <div
        ref={menu}
        className="menu menu-floating"
        role="menu"
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "ArrowUp") {
            e.preventDefault();
            move(e.key === "ArrowDown" ? 1 : -1);
          }
        }}
      >
        {items.map((item) =>
          item.heading ? (
            <p key={item.label} className="menu-label">
              {item.label}
            </p>
          ) : (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              disabled={!!item.disabled}
              title={item.disabled}
              onClick={() => {
                onClose();
                item.run?.();
              }}
            >
              {item.label}
            </button>
          ),
        )}
      </div>
    </>
  );
}
