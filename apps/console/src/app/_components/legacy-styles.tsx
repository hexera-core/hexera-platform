// The same web fonts ui/index.html requests. They are a <link> rather than next/font because the
// legacy stylesheets name the families directly, and next/font would give them generated names.
const legacyFontLinks = [
  { href: "https://fonts.googleapis.com", rel: "preconnect" },
  { crossOrigin: "anonymous", href: "https://fonts.gstatic.com", rel: "preconnect" },
  {
    href: "https://fonts.googleapis.com/css2?family=Saira+Semi+Condensed:wght@300;400;500;600&family=Cormorant:wght@600&family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap",
    precedence: "base",
    rel: "stylesheet",
  },
] as const;

// `precedence` makes each sheet a React stylesheet resource: hoisted into <head> on the server,
// and on a client navigation the new route is not shown until its sheets have loaded. A bare
// <link rel="stylesheet"> renders where it sits, in <body>, and the browser paints the page
// unstyled until it arrives. Resources are also never removed, so the three groups keep every
// sheet in the same cascade order whichever route group added it first.
const OVERLAYS = new Set(["/static/css/a11y.css", "/static/css/theme.css"]);

function precedenceOf(href: string): string {
  if (TOKENS.includes(href)) return "base";
  return OVERLAYS.has(href) ? "overlay" : "components";
}

function Sheets({ hrefs }: { hrefs: readonly string[] }) {
  return (
    <>
      {legacyFontLinks.map((link) => (
        <link key={link.href} {...link} />
      ))}
      {hrefs.map((href) => (
        <link href={href} key={href} precedence={precedenceOf(href)} rel="stylesheet" />
      ))}
    </>
  );
}

const TOKENS = ["/static/css/tokens.css", "/static/css/chrome.css"];

const AUTH_SHEETS = [...TOKENS, "/static/css/auth.css", "/static/css/a11y.css"];

// The workbench routes need the whole legacy tree: main.js, stage.js and viewer.js select on
// class names these sheets own. theme.css stays LAST -- it is the overlay that restyles the
// light-mode literals the component sheets still carry.
const DASHBOARD_SHEETS = [
  ...TOKENS,
  // auth.css FIRST, and it is not optional: /settings/api-keys and /settings/account are the
  // only forms inside the shell, and they are built from auth.css's field, input, form and error
  // primitives. Without it those two pages render browser-default inputs and an unstyled error
  // box in the middle of the dashboard.
  "/static/css/auth.css",
  "/static/css/dashboard.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/workbench.css",
  "/static/css/review.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
];

export function AuthStyles() {
  return <Sheets hrefs={AUTH_SHEETS} />;
}

export function DashboardStyles() {
  return <Sheets hrefs={DASHBOARD_SHEETS} />;
}
