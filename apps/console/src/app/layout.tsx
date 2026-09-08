import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  description: "Hexera public console",
  title: "Hexera Console",
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
