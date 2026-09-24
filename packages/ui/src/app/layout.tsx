import type { Metadata } from "next";
import AuthProvider from "@/components/AuthProvider";
import { ExecutiveStatusProvider } from "@/components/executive/ExecutiveStatusContext";
import { SessionsProvider } from "@/components/sessions/SessionsContext";
import AppShell from "@/components/shell/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Open Executive",
  description: "Your AI-powered virtual executive team",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full antialiased bg-surface text-fg">
        <AuthProvider>
          <SessionsProvider>
            <ExecutiveStatusProvider>
              <AppShell>{children}</AppShell>
            </ExecutiveStatusProvider>
          </SessionsProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
