import type { KeyboardEvent, PointerEvent } from "react";

interface Props {
  /** "row": a bar between panes stacked vertically (it changes a height); "column": side by side. */
  direction: "row" | "column";
  label: string;
  sizePx: number;
  minPx: number;
  maxPx: number;
  onResize: (px: number) => void;
  onReset: () => void;
  /** -1 when dragging up (or left) makes the pane bigger. */
  sign?: 1 | -1;
  className?: string;
  testId?: string;
}

const smallStep = 16;
const bigStep = 80;

/**
 * A bar that resizes the pane before it: drag it, double-click it for the default size, or
 * focus it and use the arrow keys (Shift for bigger steps), Home and End.
 */
export function ResizeHandle({
  direction,
  label,
  sizePx,
  minPx,
  maxPx,
  onResize,
  onReset,
  sign = 1,
  className,
  testId,
}: Props) {
  const vertical = direction === "row";
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    const start = vertical ? e.clientY : e.clientX;
    const startSize = sizePx;
    handle.setPointerCapture(e.pointerId);
    const move = (ev: globalThis.PointerEvent) =>
      onResize(startSize + sign * ((vertical ? ev.clientY : ev.clientX) - start));
    const up = (ev: globalThis.PointerEvent) => {
      if (handle.hasPointerCapture(ev.pointerId)) handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? bigStep : smallStep;
    const grow = vertical ? "ArrowDown" : "ArrowRight";
    const shrink = vertical ? "ArrowUp" : "ArrowLeft";
    let next: number | undefined;
    if (e.key === grow) next = sizePx + sign * step;
    else if (e.key === shrink) next = sizePx - sign * step;
    else if (e.key === "Home") next = minPx;
    else if (e.key === "End") next = maxPx;
    if (next === undefined) return;
    e.preventDefault();
    onResize(next);
  };
  return (
    <div
      role="separator"
      aria-orientation={vertical ? "horizontal" : "vertical"}
      aria-label={label}
      aria-valuenow={Math.round(sizePx)}
      aria-valuemin={minPx}
      aria-valuemax={Math.round(maxPx)}
      tabIndex={0}
      title="Drag to resize · double-click for the default size"
      className={`resize-handle resize-handle-${direction}${className ? ` ${className}` : ""}`}
      data-testid={testId}
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
    />
  );
}
