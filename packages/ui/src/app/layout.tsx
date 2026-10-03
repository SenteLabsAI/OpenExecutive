import type { Metadata, Viewport } from "next";
import { Geist } from "next/font/google";
import AuthProvider from "@/components/AuthProvider";
import { ExecutiveStatusProvider } from "@/components/executive/ExecutiveStatusContext";
import { SessionsProvider } from "@/components/sessions/SessionsContext";
import AppShell from "@/components/shell/AppShell";
import { WorkspaceProvider } from "@/components/workspace/WorkspaceContext";
import "./globals.css";

// Geist, self-hosted by next/font at build time: no request to Google from
// the browser, and no layout shift while it loads.
const geist = Geist({ subsets: ["latin"], variable: "--font-sans", display: "swap" });

export const metadata: Metadata = {
  title: "Open Executive",
  description: "Your AI-powered virtual executive team",
};

// `viewport-fit=cover` lets the page reach under the iPhone notch and home
// indicator; the mobile bottom bar pads itself by the safe-area inset so its
// buttons stay clear of the indicator.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`h-full ${geist.variable}`}>
      <body className="h-full antialiased bg-surface text-fg">
        <AuthProvider>
          <SessionsProvider>
            <ExecutiveStatusProvider>
              <WorkspaceProvider>
                <AppShell>{children}</AppShell>
              </WorkspaceProvider>
            </ExecutiveStatusProvider>
          </SessionsProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
