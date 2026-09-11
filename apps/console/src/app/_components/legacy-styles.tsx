// The same web fonts ui/index.html requests. They are a <link> rather than next/font because the
// legacy stylesheets name the families directly, and next/font would give them generated names.
const legacyFontLinks = [
  { href: "https://fonts.googleapis.com", rel: "preconnect" },
  { crossOrigin: "anonymous", href: "https://fonts.gstatic.com", rel: "preconnect" },
  {
    href: "https://fonts.googleapis.com/css2?family=Saira+Semi+Condensed:wght@300;400;500;600&family=Geist:wght@300;400;500;600&family=Geist+Mono:wght@400;500&display=swap",
    rel: "stylesheet",
  },
] as const;

function Sheets({ hrefs }: { hrefs: readonly string[] }) {
  return (
    <>
      {legacyFontLinks.map((link) => (
        <link key={link.href} {...link} />
      ))}
      {hrefs.map((href) => (
        <link href={href} key={href} rel="stylesheet" />
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
  "/static/css/dashboard.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/workbench.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
];

export function AuthStyles() {
  return <Sheets hrefs={AUTH_SHEETS} />;
}

export function DashboardStyles() {
  return <Sheets hrefs={DASHBOARD_SHEETS} />;
}
