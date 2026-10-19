// The Hardcharger logo (docs/assets/brand), the "by GizmoData" badge, and the trademark notice.
// Both names are pending trademarks: ™ on these prominent uses, never ®. The logo comes in a
// light-background (navy lettering) and a dark-background (white lettering) variant; the
// stylesheet shows the one that matches the theme.
import headDark from "../assets/brand/head-horizontal-dark.svg";
import headLight from "../assets/brand/head-horizontal-light.svg";
import mascotDark from "../assets/brand/mascot-stacked-dark.webp";
import mascotLight from "../assets/brand/mascot-stacked-light.webp";

// The header takes the compact horizontal lockup (the bull's head, an SVG trace), the
// sign-in page and About the stacked one with the full mascot: the original raster art as
// WebP at 480 px wide (2x the sign-in page's size), since the trace flattens its shading.
// Sizes keep each artwork's aspect ratio.
const logos = {
  header: { light: headLight, dark: headDark, width: 134, height: 30 },
  signin: { light: mascotLight, dark: mascotDark, width: 240, height: 208 },
  about: { light: mascotLight, dark: mascotDark, width: 180, height: 156 },
};

export function Wordmark({ size }: { size: "header" | "signin" | "about" }) {
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

export function CopyrightNotice() {
  return <p className="copyright-notice">© {__BUILD_YEAR__} GizmoData LLC. All rights reserved.</p>;
}

export function TrademarkNotice() {
  return <p className="tm-notice">Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.</p>;
}
