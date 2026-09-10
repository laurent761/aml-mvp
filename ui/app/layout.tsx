import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AML — Adversarial Research",
  description: "Run controlled adversarial experiments, inspect trajectories, verify exploits, and learn from outcomes.",
  other: {
    "codex-preview": "development",
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
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
