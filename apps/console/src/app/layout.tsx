import type { Metadata } from "next";

import { FirebaseConfigScript } from "@/lib/firebase/config-script";

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
      {/* Injected HERE rather than per page: `beforeInteractive` is only honoured from the root
          layout in the App Router, and every page that touches the Firebase SDK - the three auth
          pages and the console's own verification banner - needs the config before hydration. */}
      <FirebaseConfigScript />
      <body>{children}</body>
    </html>
  );
}
