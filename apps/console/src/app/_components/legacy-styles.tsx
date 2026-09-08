const legacyStylesheets = [
  "/static/css/tokens.css",
  "/static/css/shell.css",
  "/static/css/chat.css",
  "/static/css/timeline.css",
  "/static/css/result.css",
  "/static/css/viewer.css",
  "/static/css/a11y.css",
  "/static/css/theme.css",
];

export function LegacyStyles() {
  return (
    <>
      {legacyStylesheets.map((href) => (
        <link href={href} key={href} rel="stylesheet" />
      ))}
    </>
  );
}
