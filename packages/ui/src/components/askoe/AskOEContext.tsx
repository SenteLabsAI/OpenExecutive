"use client";

import { usePathname } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { PageContext, PageFormField } from "@/lib/api";

// ---------------------------------------------------------------------------
// Form registration contract
// ---------------------------------------------------------------------------

/** Result of applying a form_patch into the registered form's state. */
export interface AppliedPatch {
  /** Field names that were applied (highlighted as suggestions). */
  applied: string[];
  /** Field names the form rejected (unknown name, bad shape). */
  skipped: string[];
  /** Restores the values captured just before this patch was applied. */
  undo: () => void;
}

/**
 * What a page registers so Ask OE can describe and fill its form.
 * `getFields` is called at send time (values snapshot); `applyPatch`
 * writes proposed values into the form's controlled state and returns
 * what stuck plus an undo closure.
 */
export interface RegisteredForm {
  formId: string;
  title: string;
  description?: string;
  getFields: () => PageFormField[];
  applyPatch: (values: Record<string, unknown>) => AppliedPatch;
}

// ---------------------------------------------------------------------------
// Route → guide section + page title
// ---------------------------------------------------------------------------

// Maps a route prefix to the /guide section that documents it (ids from
// packages/core/openexecutive/guide/sections.py). Longest-prefix entries
// first where routes nest (/audit/usage before /audit). Unmapped routes
// still get explain-tier help from route + title alone.
const ROUTE_GUIDE_MAP: Array<{ prefix: string; guideId: string; title: string }> = [
  { prefix: "/audit/usage", guideId: "token_usage", title: "Token usage" },
  { prefix: "/audit", guideId: "audit", title: "Audit log" },
  { prefix: "/today", guideId: "today", title: "Today" },
  { prefix: "/memories", guideId: "pulse", title: "Pulse" },
  { prefix: "/review", guideId: "review", title: "Review queue" },
  { prefix: "/jobs", guideId: "jobs", title: "Jobs" },
  { prefix: "/artifacts", guideId: "artifacts", title: "Artifacts" },
  { prefix: "/watchlist", guideId: "watchlist", title: "Watch list" },
  { prefix: "/departments", guideId: "departments", title: "Departments" },
  { prefix: "/people", guideId: "people", title: "People" },
  { prefix: "/company-profile", guideId: "company_profile", title: "Company profile" },
  { prefix: "/knowledge", guideId: "knowledge", title: "Knowledge base" },
  { prefix: "/skills", guideId: "skills", title: "Skills" },
  { prefix: "/council", guideId: "council", title: "Agent Council" },
  { prefix: "/demo", guideId: "simulator", title: "Company Simulator" },
  { prefix: "/settings", guideId: "settings", title: "Settings" },
];

// Routes with no guide section yet — still want a readable page title.
const EXTRA_TITLES: Array<{ prefix: string; title: string }> = [
  { prefix: "/clients", title: "Client Companies" },
  { prefix: "/architecture", title: "Architecture" },
  { prefix: "/guide", title: "User Guide" },
];

function resolveRouteMeta(pathname: string): { guideId: string | null; title: string } {
  for (const entry of ROUTE_GUIDE_MAP) {
    if (pathname === entry.prefix || pathname.startsWith(`${entry.prefix}/`)) {
      return { guideId: entry.guideId, title: entry.title };
    }
  }
  for (const entry of EXTRA_TITLES) {
    if (pathname === entry.prefix || pathname.startsWith(`${entry.prefix}/`)) {
      return { guideId: null, title: entry.title };
    }
  }
  const first = pathname.split("/").filter(Boolean)[0] ?? "";
  return { guideId: null, title: first || "Open Executive" };
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface AskOEContextValue {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
  /** Identity of the currently registered form, for panel UI. */
  formMeta: { formId: string; title: string } | null;
  /** Latest registered form (fresh closures) — used by the panel. */
  getForm: () => RegisteredForm | null;
  registerForm: (form: RegisteredForm) => () => void;
  /** Keep the registered form's closures fresh across re-renders. */
  refreshForm: (form: RegisteredForm) => void;
  suggested: Set<string>;
  markSuggested: (names: string[]) => void;
  clearSuggested: (name?: string) => void;
  buildPageContext: () => PageContext;
}

const AskOECtx = createContext<AskOEContextValue | null>(null);

export function AskOEProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const [open, setOpen] = useState(false);
  const [formMeta, setFormMeta] = useState<{ formId: string; title: string } | null>(null);
  const [suggested, setSuggested] = useState<Set<string>>(new Set());
  const formRef = useRef<RegisteredForm | null>(null);

  const toggle = useCallback(() => setOpen((v) => !v), []);

  // Ctrl/Cmd + . toggles the panel from anywhere in the shell.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "." && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        toggle();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggle]);

  const registerForm = useCallback((form: RegisteredForm) => {
    formRef.current = form;
    setFormMeta({ formId: form.formId, title: form.title });
    return () => {
      // Only clear if a different form hasn't registered in the meantime
      // (e.g. navigating from one form page straight to another).
      if (formRef.current?.formId === form.formId) {
        formRef.current = null;
        setFormMeta(null);
        setSuggested(new Set());
      }
    };
  }, []);

  const refreshForm = useCallback((form: RegisteredForm) => {
    if (formRef.current?.formId === form.formId) {
      formRef.current = form;
    }
  }, []);

  const getForm = useCallback(() => formRef.current, []);

  const markSuggested = useCallback((names: string[]) => {
    if (names.length === 0) return;
    setSuggested((prev) => new Set([...prev, ...names]));
  }, []);

  const clearSuggested = useCallback((name?: string) => {
    setSuggested((prev) => {
      if (name === undefined) return new Set();
      if (!prev.has(name)) return prev;
      const next = new Set(prev);
      next.delete(name);
      return next;
    });
  }, []);

  const buildPageContext = useCallback((): PageContext => {
    const meta = resolveRouteMeta(pathname);
    const form = formRef.current;
    return {
      route: pathname,
      title: meta.title,
      guide_section_id: meta.guideId,
      form: form
        ? {
            form_id: form.formId,
            title: form.title,
            description: form.description,
            fields: form.getFields(),
          }
        : null,
    };
  }, [pathname]);

  const value = useMemo<AskOEContextValue>(
    () => ({
      open,
      setOpen,
      toggle,
      formMeta,
      getForm,
      registerForm,
      refreshForm,
      suggested,
      markSuggested,
      clearSuggested,
      buildPageContext,
    }),
    [
      open,
      toggle,
      formMeta,
      getForm,
      registerForm,
      refreshForm,
      suggested,
      markSuggested,
      clearSuggested,
      buildPageContext,
    ]
  );

  return <AskOECtx.Provider value={value}>{children}</AskOECtx.Provider>;
}

export function useAskOE(): AskOEContextValue {
  const ctx = useContext(AskOECtx);
  if (!ctx) throw new Error("useAskOE must be used inside <AskOEProvider>");
  return ctx;
}

/** Tailwind classes marking an input as an OE suggestion (cleared on edit). */
export const SUGGESTED_CLS = "ring-1 ring-indigo-400/70 bg-indigo-500/10";

/**
 * Register a form with the Ask OE panel for the lifetime of the calling
 * component. Pass `null` to skip registration (e.g. modal closed).
 *
 * Returns helpers for the suggested-value highlight: append
 * `suggestedCls(name)` to an input's className and call
 * `clearSuggested(name)` in its onChange so the highlight clears the
 * moment the user edits the field.
 */
export function useAskOEFormContext(form: RegisteredForm | null): {
  suggestedCls: (name: string) => string;
  clearSuggested: (name: string) => void;
} {
  const ctx = useContext(AskOECtx);
  const formId = form?.formId ?? null;

  // Register once per formId; the cleanup unregisters on unmount/close.
  useEffect(() => {
    if (!ctx || !form) return;
    return ctx.registerForm(form);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-register only when the form identity changes, not on every closure refresh
  }, [ctx?.registerForm, formId]);

  // Keep closures (getFields/applyPatch) fresh on every render so the
  // panel always snapshots current values without re-registering.
  useEffect(() => {
    if (ctx && form) ctx.refreshForm(form);
  });

  const suggestedCls = useCallback(
    (name: string) => (ctx?.suggested.has(name) ? SUGGESTED_CLS : ""),
    [ctx?.suggested]
  );
  const clearSuggested = useCallback(
    (name: string) => ctx?.clearSuggested(name),
    [ctx?.clearSuggested]
  );

  return { suggestedCls, clearSuggested };
}
