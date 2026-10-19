// The Hardcharger logo (docs/assets/brand), the "by GizmoData" badge, and the trademark notice.
// Both names are pending trademarks: ™ on these prominent uses, never ®. The logo comes in a
// light-background (navy lettering) and a dark-background (white lettering) variant; the
// stylesheet shows the one that matches the theme.
import headDark from "../assets/brand/head-horizontal-dark.svg";
import headLight from "../assets/brand/head-horizontal-light.svg";
import mascotDark from "../assets/brand/mascot-stacked-dark.svg";
import mascotLight from "../assets/brand/mascot-stacked-light.svg";

// The header takes the compact horizontal lockup (the bull's head), the sign-in page the
// stacked one with the full mascot. Sizes keep each artwork's aspect ratio.
const logos = {
  header: { light: headLight, dark: headDark, width: 134, height: 30 },
  signin: { light: mascotLight, dark: mascotDark, width: 240, height: 208 },
};

export function Wordmark({ size }: { size: "header" | "signin" }) {
  const logo = logos[size];
  return (
    <span className={`wordmark wordmark-${size}`}>
      <img className="logo-light" src={logo.light} alt="Hardcharger™" width={logo.width} height={logo.height} />
      <img className="logo-dark" src={logo.dark} alt="Hardcharger™" width={logo.width} height={logo.height} />
      <span className="tm" aria-hidden="true">
        ™
      </span>
    </span>
  );
}

export function ByGizmoData() {
  return (
    <a className="by-badge" href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">
      <span className="by-badge-by">by</span>
      <span className="by-badge-name">GizmoData™</span>
    </a>
  );
}

export function TrademarkNotice() {
  return <p className="tm-notice">Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.</p>;
}
