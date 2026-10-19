import { useEffect, useRef } from "react";

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
        style={{
          ...(align === "right"
            ? { right: Math.max(8, window.innerWidth - x) }
            : { left: Math.min(x, window.innerWidth - 240) }),
          top: Math.min(y, window.innerHeight - items.length * 30 - 16),
        }}
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
