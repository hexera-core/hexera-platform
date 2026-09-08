import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  description: "Hexera admin console",
  title: "Hexera Admin",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
