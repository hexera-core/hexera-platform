const legacyStylesheets = [
  "/static/css/tokens.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
  // Added by the mesh-quality campaign (#11): the delivered mesh gets a workbench and the
  // conversation becomes a drawer beside it. ui/index.html loads this; the console renders its
  // own page, so it has to load it here or the workbench is unstyled.
  "/static/css/workbench.css",
];

// The same web fonts ui/index.html requests. They are a <link> rather than next/font because the
// legacy stylesheets name the families directly, and next/font would give them generated names.
const legacyFontLinks = [
  { href: "https://fonts.googleapis.com", rel: "preconnect" },
  { crossOrigin: "anonymous", href: "https://fonts.gstatic.com", rel: "preconnect" },
  {
    href: "https://fonts.googleapis.com/css2?family=Saira+Semi+Condensed:wght@400;500;600&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap",
    rel: "stylesheet",
  },
] as const;

export function LegacyStyles() {
  return (
    <>
      {legacyFontLinks.map((link) => (
        <link key={link.href} {...link} />
      ))}
      {legacyStylesheets.map((href) => (
        <link href={href} key={href} rel="stylesheet" />
      ))}
    </>
  );
}
