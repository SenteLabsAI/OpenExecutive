"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import Icon from "@/components/Icon";
import { SETTINGS_TABS, type SettingsControl, type SettingsTab } from "@/components/settings/registry";
import { ADVANCED_ITEMS } from "@/components/shell/navConfig";

function tabHref(id: string) {
  return id === "general" ? "/settings" : `/settings/${id}`;
}

function activeTabId(pathname: string): string {
  if (pathname === "/settings") return "general";
  const parts = pathname.split("/").filter(Boolean);
  return parts[1] || "general";
}

function PhaseBadge({ phase, live }: { phase: number; live: boolean }) {
  if (live) {
    return (
      <span className="text-[10px] uppercase tracking-wide text-emerald-400/90 border border-emerald-500/20 rounded px-1.5 py-0.5">
        F{phase} live
      </span>
    );
  }
  return (
    <span className="text-[10px] uppercase tracking-wide text-fg-muted border border-line rounded px-1.5 py-0.5">
      reserved · F{phase}
    </span>
  );
}

function ControlRow({ control }: { control: SettingsControl }) {
  const disabled = Boolean(control.locked) || control.liveInPhase > 1;
  const value =
    control.defaultValue === undefined || control.defaultValue === null
      ? ""
      : String(control.defaultValue);

  if (control.kind === "link" && control.href) {
    return (
      <Link
        href={control.href}
        className="flex items-center justify-between gap-3 rounded-lg border border-line bg-surface-elevated px-3 py-2.5 hover:border-line-strong hover:bg-surface-overlay transition-colors"
      >
        <div>
          <div className="text-sm text-fg">{control.label}</div>
          <div className="text-xs text-fg-muted mt-0.5">{control.hint}</div>
        </div>
        <span className="text-xs text-indigo-400">Open →</span>
      </Link>
    );
  }

  return (
    <div className="rounded-lg border border-line bg-surface-elevated px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm text-fg">{control.label}</div>
          <div className="text-xs text-fg-muted mt-0.5">{control.hint}</div>
          {control.lockedReason && (
            <div className="text-[11px] text-amber-400/80 mt-1">{control.lockedReason}</div>
          )}
          {control.restartRequired && (
            <div className="text-[11px] text-fg-muted mt-1">Restart required to apply.</div>
          )}
        </div>
        <div className="shrink-0">
          {control.kind === "toggle" && (
            <button
              type="button"
              disabled={disabled}
              aria-pressed={control.defaultValue === true}
              className={`h-6 w-10 rounded-full border transition-colors ${
                control.defaultValue
                  ? "bg-indigo-500/80 border-indigo-400/40"
                  : "bg-surface border-line"
              } ${disabled ? "opacity-50 cursor-not-allowed" : ""}`}
            >
              <span
                className={`block h-5 w-5 rounded-full bg-white/90 mt-[1px] transition-transform ${
                  control.defaultValue ? "translate-x-[18px]" : "translate-x-[1px]"
                }`}
              />
            </button>
          )}
          {control.kind === "select" && (
            <select
              disabled={disabled}
              defaultValue={value}
              className="text-xs bg-surface border border-line rounded-md px-2 py-1 text-fg disabled:opacity-50"
            >
              {(control.options || [value]).map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          )}
          {control.kind === "text" && (
            <input
              disabled={disabled}
              defaultValue={value}
              className="text-xs bg-surface border border-line rounded-md px-2 py-1 text-fg w-44 disabled:opacity-50"
            />
          )}
          {control.kind === "number" && (
            <input
              type="number"
              disabled={disabled}
              defaultValue={value}
              className="text-xs bg-surface border border-line rounded-md px-2 py-1 text-fg w-24 disabled:opacity-50"
            />
          )}
          {(control.kind === "readonly" || control.kind === "secret-status" || control.kind === "list") && (
            <span className="text-xs text-fg-muted">
              {control.kind === "secret-status" ? "unset" : value || "—"}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export function SettingsTabNav() {
  const pathname = usePathname() || "/settings";
  const active = activeTabId(pathname);

  return (
    <nav className="flex gap-1 overflow-x-auto pb-1 -mx-1 px-1">
      {SETTINGS_TABS.map((tab) => {
        const isActive = tab.id === active;
        return (
          <Link
            key={tab.id}
            href={tabHref(tab.id)}
            className={`shrink-0 inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs border transition-colors ${
              isActive
                ? "border-line-strong bg-surface-overlay text-fg"
                : "border-transparent text-fg-muted hover:text-fg hover:bg-surface-elevated"
            }`}
          >
            <Icon name={tab.icon} size="w-3.5 h-3.5" />
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}

export function SettingsTabBody({ tab }: { tab: SettingsTab }) {
  const live = tab.phase === 1;
  return (
    <section className="mt-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-fg">{tab.label}</h1>
          <p className="mt-1 text-sm text-fg-muted">{tab.description}</p>
        </div>
        <PhaseBadge phase={tab.phase} live={live} />
      </div>
      {!live && (
        <p className="mt-3 text-xs text-fg-muted border border-line rounded-lg px-3 py-2">
          This tab is reserved so we do not add routes later. Controls stay visible and disabled until Phase {tab.phase}.
        </p>
      )}
      <div className="mt-4 grid gap-2">
        {tab.controls.map((c) => (
          <ControlRow key={c.id} control={c} />
        ))}
      </div>
    </section>
  );
}

export function SettingsAdvancedDirectory() {
  return (
    <section className="mt-10">
      <h2 className="text-sm font-medium text-fg">Open Executive tools</h2>
      <p className="mt-1 text-xs text-fg-muted">
        Existing OE pages — kept as-is. Settings tabs wrap them instead of replacing them.
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {ADVANCED_ITEMS.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className="group rounded-xl border border-line bg-surface-elevated p-4 hover:border-line-strong hover:bg-surface-overlay transition-colors"
          >
            <div className="flex items-center gap-2.5">
              <span className="text-fg-muted group-hover:text-fg transition-colors">
                <Icon name={item.icon} size="w-5 h-5" />
              </span>
              <h3 className="text-sm font-medium text-fg">{item.label}</h3>
            </div>
            <p className="mt-2 text-xs text-fg-muted leading-relaxed">{item.description}</p>
          </Link>
        ))}
      </div>
    </section>
  );
}
