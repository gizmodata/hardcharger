// The README banner's marks, in the UI: the GizmoData logo with the Hardcharger wordmark over
// its orange-to-blue bar, and the "by GizmoData" badge.
import logo from "../assets/gizmodata-logo.png";

export function Wordmark({ size }: { size: "header" | "signin" }) {
  const [w, h] = size === "header" ? [26, 28] : [46, 49];
  return (
    <span className={`wordmark wordmark-${size}`}>
      <img src={logo} alt="GizmoData" width={w} height={h} />
      <span className="wordmark-text">
        <span className="wordmark-name">Hardcharger</span>
        <span className="wordmark-bar" aria-hidden="true" />
      </span>
    </span>
  );
}

export function ByGizmoData() {
  return (
    <a className="by-badge" href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">
      <span className="by-badge-by">by</span>
      <span className="by-badge-name">GizmoData</span>
    </a>
  );
}
