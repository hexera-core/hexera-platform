/* eslint-disable @next/next/no-img-element */
import Link from "next/link";

//: hexera.ai. The auth pages are the seam between the marketing site and the product, so their
//: brand mark goes BACK to where the user came from rather than to the console root they cannot
//: reach yet.
const SITE_URL = "https://hexera.ai";

export function AuthChrome({ children }: { children: React.ReactNode }) {
  return (
    <>
      <div aria-hidden="true" className="page-bg" />
      <div aria-hidden="true" className="grain" />
      <header className="nav">
        <a aria-label="Hexera home" className="brand" href={SITE_URL}>
          <img alt="" className="brand__mark" src="/static/assets/logo.png" />
          <span className="brand__word">HEXERA</span>
        </a>
        <div className="nav__end" style={{ display: "flex", alignItems: "center", gap: "1.4rem" }}>
          <a className="nav__link" href={`${SITE_URL}/contact.html`}>
            Contact
          </a>
          <Link className="btn btn--ghost cta-mono" href="/sign-in">
            Sign in
          </Link>
        </div>
      </header>
      <main className="auth">{children}</main>
    </>
  );
}
