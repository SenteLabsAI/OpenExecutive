"use client";

import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";

// A page that shows one record (a person, a department) names it in the
// breadcrumb instead of the raw id from the URL.

type Crumb = { path: string; label: string } | null;

const PageCrumbContext = createContext<{
  crumb: Crumb;
  setCrumb: (c: Crumb) => void;
}>({ crumb: null, setCrumb: () => undefined });

export function PageCrumbProvider({ children }: { children: React.ReactNode }) {
  const [crumb, setCrumb] = useState<Crumb>(null);
  return <PageCrumbContext.Provider value={{ crumb, setCrumb }}>{children}</PageCrumbContext.Provider>;
}

/** The label set for this path, if a page set one. */
export function usePageCrumbLabel(pathname: string): string | null {
  const { crumb } = useContext(PageCrumbContext);
  return crumb && crumb.path === pathname ? crumb.label : null;
}

/** Names the last breadcrumb for the current page while it is mounted. */
export function usePageCrumb(label: string | null | undefined): void {
  const pathname = usePathname() ?? "";
  const { setCrumb } = useContext(PageCrumbContext);
  useEffect(() => {
    if (!label) return;
    setCrumb({ path: pathname, label });
    return () => setCrumb(null);
  }, [label, pathname, setCrumb]);
}
