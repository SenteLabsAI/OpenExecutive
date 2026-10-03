"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  AgentDetail,
  AgentHistoryEntry,
  AgentMeta,
  Persona,
  ModelOption,
  PersonaMeta,
  QualityPresetId,
  QualityPresets,
  applyQualityPreset,
  createPersona,
  deletePersona,
  getAgentDetail,
  getPersona,
  listAgentHistory,
  listAgentModelOptions,
  listAgents,
  listPersonas,
  listQualityPresets,
  patchAgent,
  resetAgent,
  resetPersona,
  rollbackAgent,
  savePersona,
  testAgent,
} from "@/lib/api";
import PageSideNav from "@/components/shell/PageSideNav";
import Button from "@/components/ui/Button";
import OverflowMenu from "@/components/ui/OverflowMenu";

interface DraftState {
  role: string;
  model: string;
  deep_reasoning: boolean;
  prompt: string;
  voice_persona_slug: string | null;
  research_focus: string | null;
  instructions: string;
}

// Mirrors INSTRUCTIONS_MAX_CHARS in api/routes/agents.py.
const INSTRUCTIONS_MAX_CHARS = 4000;

// Haiku is the one Claude family that rejects adaptive thinking (HTTP 400),
// so the deep-reasoning toggle is disabled for it whether the slug is the
// Anthropic id (claude-haiku-4-5) or the OpenRouter form
// (anthropic/claude-haiku-4.5). Mirrors the backend guard in
// providers.registry.model_supports_deep_reasoning.
// Scoped to Claude names (current or legacy ordering), case-insensitive —
// matches providers.registry.model_supports_deep_reasoning exactly.
const HAIKU_MODEL_RE = /^(anthropic\/)?claude-.*haiku/i;

function modelSupportsDeepReasoning(model: string): boolean {
  return !HAIKU_MODEL_RE.test(model);
}

// A model in the picker that the backend allowlist doesn't carry: a stale
// override (claude-opus-4-7) or a default whose family isn't reachable in
// this deployment. It stays selectable so the current value is never hidden.
type PickerOption = ModelOption & { unlisted?: boolean };

function unlistedOption(id: string, known: ModelOption[]): PickerOption {
  const slash = id.indexOf("/");
  const provider = /^(anthropic\/)?claude-/i.test(id)
    ? "anthropic"
    : slash > 0
      ? id.slice(0, slash)
      : "other";
  const sibling = known.find((o) => o.provider === provider);
  const fallbackLabel = provider === "anthropic" ? "Anthropic" : provider === "other" ? "Other" : provider;
  return {
    id,
    provider,
    provider_label: sibling?.provider_label ?? fallbackLabel,
    route: sibling?.route ?? "openrouter",
    label: id,
    unlisted: true,
  };
}

const ROUTE_TEXT: Record<ModelOption["route"], string> = {
  direct: "Anthropic API",
  openrouter: "via OpenRouter",
  local: "local backend",
};

function personaOption(p: PersonaMeta) {
  return (
    <option key={p.slug} value={p.slug}>
      {p.display_name}
      {p.is_builtin && !p.is_customized ? " · built-in" : ""}
      {p.is_customized ? " · customized" : ""}
      {!p.is_builtin ? " · custom" : ""}
    </option>
  );
}

// Remembers, per browser, that the owner prefers the full editor.
const ADVANCED_KEY = "oe.council.advanced";

// The full editor's tabs for the selected agent. Each shows only where the
// agent has something on it: the utility and research knobs have just a
// model, and the utility one can't be test-run.
type EditorTab = "model" | "instructions" | "prompt" | "test" | "history";

const TAB_LABEL: Record<EditorTab, string> = {
  model: "Model",
  instructions: "Instructions",
  prompt: "Prompt",
  test: "Test",
  history: "History",
};

function editorTabs(d: AgentDetail): EditorTab[] {
  const knob = d.name === "utility_fast" || d.name === "research";
  const tabs: EditorTab[] = ["model"];
  if (!knob) tabs.push("instructions");
  if (!knob || d.research_focus_default !== null || d.name === "executive") tabs.push("prompt");
  if (d.name !== "utility_fast") tabs.push("test");
  tabs.push("history");
  return tabs;
}

const FIELD =
  "px-3.5 rounded-xl bg-surface border border-line text-fg focus:border-accent/60 focus:outline-none";
const FIELD_LABEL = "block text-sm font-semibold text-fg";

function detailToDraft(d: AgentDetail): DraftState {
  return {
    role: d.role,
    model: d.model,
    // A stored override can pair deep_reasoning=true with a Haiku model
    // (older UI, direct API call). Mask it on load so the checkbox, the
    // dirty check and the Save patch all agree — saving then corrects the
    // persisted row instead of leaving it permanently out of sync.
    deep_reasoning: d.deep_reasoning && modelSupportsDeepReasoning(d.model),
    prompt: d.prompt,
    voice_persona_slug: d.voice_persona_slug ?? null,
    research_focus: d.research_focus ?? null,
    instructions: d.instructions ?? "",
  };
}

function draftIsDirty(d: AgentDetail | null, draft: DraftState | null): boolean {
  if (!d || !draft) return false;
  return (
    d.role !== draft.role ||
    d.model !== draft.model ||
    d.deep_reasoning !== draft.deep_reasoning ||
    d.prompt !== draft.prompt ||
    (d.voice_persona_slug ?? null) !== draft.voice_persona_slug ||
    (d.research_focus ?? null) !== draft.research_focus ||
    (d.instructions ?? "") !== draft.instructions
  );
}

export default function CouncilPage() {
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [history, setHistory] = useState<AgentHistoryEntry[]>([]);
  const [tab, setTab] = useState<EditorTab>("model");

  const [presets, setPresets] = useState<QualityPresets | null>(null);
  // The Council opens in its simple view: Quality and the core agents with
  // their additional instructions (the voice is chosen in Settings → Your
  // Executive). "Show all agents" lists the
  // internal ones too; "Advanced" opens the full editor, and this browser
  // remembers that choice.
  const [showAll, setShowAll] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const simple = !advanced;
  const [applyingPreset, setApplyingPreset] = useState<QualityPresetId | null>(null);

  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [testQuery, setTestQuery] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);

  // Voice persona state
  const [personas, setPersonas] = useState<PersonaMeta[]>([]);
  const [activePersonaDetail, setActivePersonaDetail] = useState<Persona | null>(null);
  const [personaBodyDraft, setPersonaBodyDraft] = useState<string>("");
  const [personaDisplayNameDraft, setPersonaDisplayNameDraft] = useState<string>("");
  const [savingPersona, setSavingPersona] = useState(false);
  const [personaError, setPersonaError] = useState<string | null>(null);
  const [newPersonaMode, setNewPersonaMode] = useState(false);
  const [newPersonaName, setNewPersonaName] = useState("");
  const [newPersonaBody, setNewPersonaBody] = useState("");

  const refreshAgents = useCallback(async () => {
    // Any agent change can move the council on or off a preset.
    listQualityPresets().then(setPresets).catch(() => {});
    try {
      const list = await listAgents();
      setAgents(list);
      if (selected === null && list.length > 0) setSelected(list[0].name);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load agents");
    }
  }, [selected]);

  const loadDetail = useCallback(async (name: string) => {
    setError(null);
    try {
      const d = await getAgentDetail(name);
      setDetail(d);
      setDraft(detailToDraft(d));
      setTestResult(null);
      setTestError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load agent");
    }
  }, []);

  useEffect(() => {
    refreshAgents();
    listPersonas().then(setPersonas).catch(() => {});
  }, [refreshAgents]);

  useEffect(() => {
    try {
      if (window.localStorage.getItem(ADVANCED_KEY) === "1") setAdvanced(true);
    } catch {
      // Storage can be blocked; the page then opens in the simple view.
    }
  }, []);

  const toggleAdvanced = () => {
    const next = !advanced;
    setAdvanced(next);
    try {
      window.localStorage.setItem(ADVANCED_KEY, next ? "1" : "0");
    } catch {
      // Not remembered; the toggle still works for this visit.
    }
    if (!selected) return;
    // The voice may have changed in Settings since this page loaded, so pick
    // up the stored one. Unsaved edits stay in the draft; only a clean draft
    // is reloaded.
    const keepDraft = dirty;
    getAgentDetail(selected)
      .then((d) => {
        setDetail(d);
        setDraft((prev) =>
          keepDraft && prev ? { ...prev, voice_persona_slug: d.voice_persona_slug ?? null } : detailToDraft(d),
        );
      })
      .catch(() => {});
  };

  const listedAgents = simple && !showAll ? agents.filter((a) => a.visibility === "core") : agents;

  useEffect(() => {
    if (selected) loadDetail(selected);
    // Refetch the model allowlist when the agent changes (every agent
    // currently gets the same list).
    listAgentModelOptions(selected ?? undefined).then(setModelOptions).catch(() => {});
  }, [selected, loadDetail]);

  // The tab stays put when another agent is picked, unless that agent has no
  // such tab; then the editor opens on Model.
  const tabs = detail ? editorTabs(detail) : (["model"] as EditorTab[]);
  const activeTab = tabs.includes(tab) ? tab : "model";
  const historyOpen = advanced && activeTab === "history";

  useEffect(() => {
    if (!selected || !historyOpen) return;
    listAgentHistory(selected).then(setHistory).catch(() => setHistory([]));
  }, [selected, historyOpen]);

  // Load persona detail whenever the draft persona slug changes (executive only)
  useEffect(() => {
    if (selected !== "executive" || !draft) return;
    const slug = draft.voice_persona_slug ?? "default";
    getPersona(slug)
      .then((p) => {
        setActivePersonaDetail(p);
        setPersonaBodyDraft(p.body);
        setPersonaDisplayNameDraft(p.display_name);
        setPersonaError(null);
      })
      .catch(() => setPersonaError("Failed to load persona"));
  }, [selected, draft?.voice_persona_slug]);

  const pickerOptions = useMemo<PickerOption[]>(() => {
    const known = new Set(modelOptions.map((o) => o.id));
    const extra = Array.from(
      new Set([detail?.model_default, draft?.model].filter((m): m is string => !!m))
    )
      .filter((m) => !known.has(m))
      .map((m) => unlistedOption(m, modelOptions));
    return [...modelOptions, ...extra];
  }, [modelOptions, detail?.model_default, draft?.model]);

  // Provider groups in first-seen order (backend order: Anthropic first).
  const providerGroups = useMemo(() => {
    const groups = new Map<string, string>();
    for (const o of pickerOptions) {
      if (!groups.has(o.provider)) groups.set(o.provider, o.provider_label);
    }
    return Array.from(groups, ([provider, label]) => ({ provider, label }));
  }, [pickerOptions]);

  const currentModel = pickerOptions.find((o) => o.id === draft?.model);

  const selectModel = (model: string) => {
    if (!draft) return;
    setDraft({
      ...draft,
      model,
      deep_reasoning: modelSupportsDeepReasoning(model) ? draft.deep_reasoning : false,
    });
  };

  // Switching provider picks the agent's default if it lives there, else the
  // provider's first (newest) model.
  const selectProvider = (provider: string) => {
    const inGroup = pickerOptions.filter((o) => o.provider === provider);
    const next = inGroup.find((o) => o.id === detail?.model_default) ?? inGroup[0];
    if (next) selectModel(next.id);
  };

  const dirty = draftIsDirty(detail, draft);

  const handleSave = async () => {
    if (!detail || !draft || !selected) return;
    setSaving(true);
    setError(null);
    try {
      // Send only the fields that differ from the defaults OR differ from
      // the current effective value, so we don't write redundant overrides.
      const patch: Record<string, unknown> = {};
      if (draft.role !== detail.role) {
        patch.role = draft.role === detail.role_default ? null : draft.role;
      }
      if (draft.model !== detail.model) {
        patch.model = draft.model === detail.model_default ? null : draft.model;
      }
      if (draft.deep_reasoning !== detail.deep_reasoning) {
        patch.use_deep_reasoning =
          draft.deep_reasoning === detail.deep_reasoning_default
            ? null
            : draft.deep_reasoning;
      }
      if (draft.prompt !== detail.prompt) {
        patch.prompt = draft.prompt === detail.prompt_default ? null : draft.prompt;
      }
      if ((draft.voice_persona_slug ?? null) !== (detail.voice_persona_slug ?? null)) {
        patch.voice_persona_slug = draft.voice_persona_slug;
      }
      if ((draft.research_focus ?? null) !== (detail.research_focus ?? null)) {
        // Sending the code default as null clears the override (back to default).
        patch.research_focus =
          draft.research_focus === (detail.research_focus_default ?? null)
            ? null
            : draft.research_focus;
      }
      if (draft.instructions !== (detail.instructions ?? "")) {
        // Blank clears the field; the backend stores null for "".
        patch.instructions = draft.instructions.trim() ? draft.instructions : null;
      }
      const updated = await patchAgent(selected, patch);
      setDetail(updated);
      setDraft(detailToDraft(updated));
      refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  const handleApplyPreset = async (id: QualityPresetId) => {
    const preset = presets?.presets.find((p) => p.id === id);
    if (!preset) return;
    if (
      !window.confirm(
        `Switch every agent to ${preset.label}? This changes each agent's model and deep reasoning; ` +
          "prompts and instructions stay as they are, and earlier settings move to history."
      )
    )
      return;
    setApplyingPreset(id);
    setError(null);
    try {
      setPresets(await applyQualityPreset(id));
      await refreshAgents();
      if (selected) await loadDetail(selected);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to apply preset");
    } finally {
      setApplyingPreset(null);
    }
  };

  const customAgents = new Set(presets?.custom_agents ?? []);
  const basePresetLabel = presets?.presets.find((p) => p.id === presets.base)?.label;

  const handleReset = async () => {
    if (!selected) return;
    if (!window.confirm("Reset this agent to defaults? Current override will move to history.")) return;
    setResetting(true);
    setError(null);
    try {
      await resetAgent(selected);
      await loadDetail(selected);
      refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset failed");
    } finally {
      setResetting(false);
    }
  };

  const handleRollback = async (historyId: number) => {
    if (!selected) return;
    if (!window.confirm("Restore this earlier version?")) return;
    try {
      const updated = await rollbackAgent(selected, historyId);
      setDetail(updated);
      setDraft(detailToDraft(updated));
      refreshAgents();
      const fresh = await listAgentHistory(selected);
      setHistory(fresh);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rollback failed");
    }
  };

  const handleTest = async () => {
    if (!selected || !draft || !testQuery.trim()) return;
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      const result = await testAgent(selected, {
        query: testQuery,
        prompt: draft.prompt,
        instructions: draft.instructions,
        model: draft.model,
        use_deep_reasoning: draft.deep_reasoning && modelSupportsDeepReasoning(draft.model),
      });
      setTestResult(result.response);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "Test failed");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="flex flex-col md:flex-row flex-1 min-h-0 bg-surface text-fg overflow-hidden">
      <PageSideNav
        label="Agent"
        current={listedAgents.find((a) => a.name === selected)?.role}
        closeKey={selected ?? ""}
        className="md:w-64 bg-surface-elevated"
      >
        <div className="px-3 py-4 overflow-y-auto">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            Agent Council
          </p>
          <nav className="space-y-0.5">
            {listedAgents.map((a) => (
              <button
                key={a.name}
                data-closes-nav
                onClick={() => setSelected(a.name)}
                className={`w-full text-left flex items-start gap-2.5 px-3 py-2.5 rounded-xl text-[15px] md:text-sm transition-colors ${
                  selected === a.name
                    ? "bg-accent/10 text-accent"
                    : "text-fg-muted hover:text-fg hover:bg-surface-overlay/60"
                }`}
              >
                <span
                  className={`mt-1.5 inline-block w-2 h-2 rounded-full flex-shrink-0 ${
                    a.has_override ? "bg-amber-400" : "bg-surface-input"
                  }`}
                  title={a.has_override ? "Has override" : "Default config"}
                />
                <span className="flex-1 min-w-0">
                  <span className="block truncate font-medium text-fg">{a.role}</span>
                  <span className="block font-mono text-[11px] text-fg-subtle">{a.name}</span>
                </span>
                {customAgents.has(a.name) && (
                  <span
                    className="mt-0.5 text-[9px] uppercase tracking-widest px-1 rounded bg-surface-overlay text-fg-subtle"
                    title={`Differs from the ${basePresetLabel ?? ""} preset`}
                  >
                    Custom
                  </span>
                )}
              </button>
            ))}
          </nav>
          {simple && (
            <Button variant="ghost" size="sm" className="mt-2 w-full" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "Show fewer agents" : "Show all agents"}
            </Button>
          )}
        </div>
      </PageSideNav>

      <main className="flex-1 min-h-0 min-w-0 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-4 py-6 sm:px-8 sm:py-10 space-y-6">
          <div>
            <div className="flex items-start justify-between gap-4">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">Agent Council</h1>
              <Button variant="secondary" size="sm" onClick={toggleAdvanced}>
                {advanced ? "Back to simple view" : "Advanced"}
              </Button>
            </div>
            <p className="mt-2 text-[15px] text-fg-muted">
              {simple
                ? "Pick how thorough answers should be and add instructions for any agent. Changes apply on the next message."
                : "Edit each specialist\u2019s prompt, model, and behavior. Changes apply on the next specialist call — no restart needed. Resetting restores the built-in defaults."}
            </p>
          </div>

          {error && (
            <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">
              {error}
            </div>
          )}

          {presets && (
            <section className="rounded-2xl border border-line bg-surface-elevated p-5 sm:p-6 space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-fg">Quality</h2>
                {presets.active === null && (
                  <span
                    className="text-[10px] uppercase tracking-widest px-2 py-1 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20"
                    title={`${presets.custom_agents.length} agent(s) differ from ${basePresetLabel ?? "the preset"}`}
                  >
                    Custom
                  </span>
                )}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {presets.presets.map((p) => {
                  const current = presets.active === p.id;
                  const base = presets.active === null && presets.base === p.id;
                  return (
                    <button
                      key={p.id}
                      onClick={() => handleApplyPreset(p.id)}
                      disabled={!p.available || current || applyingPreset !== null}
                      aria-pressed={current}
                      title={p.available ? undefined : "This install offers none of this preset's models"}
                      className={`flex flex-col items-start justify-start text-left rounded-2xl border-2 p-4 transition-colors disabled:cursor-not-allowed ${
                        current
                          ? "border-accent bg-accent/10"
                          : base
                            ? "border-accent/40 border-dashed"
                            : "border-line hover:border-accent/50"
                      } ${p.available ? "" : "opacity-40"}`}
                    >
                      <span className="block text-base font-semibold text-fg">
                        {applyingPreset === p.id ? "Applying…" : p.label}
                      </span>
                      <span className="block text-sm text-fg-muted mt-1 leading-relaxed">{p.description}</span>
                      {p.model && (
                        <span className="block text-[11px] text-fg-subtle mt-2 font-mono">{p.model}</span>
                      )}
                    </button>
                  );
                })}
              </div>
              <p className="text-[13px] text-fg-subtle">
                A preset sets every agent&apos;s model and deep reasoning at once. Changing one agent
                below marks it Custom; each agent keeps its own history.
              </p>
            </section>
          )}

          {simple && (
            <Link
              href="/settings/executive"
              className="group flex items-center justify-between gap-3 rounded-2xl border border-line bg-surface-elevated px-5 py-4 text-[15px] text-fg-muted transition-colors hover:border-accent/50 hover:text-fg"
            >
              <span>
                Voice is set in <span className="font-semibold text-fg">Settings → Your Executive</span>
              </span>
              <span aria-hidden="true" className="text-fg-subtle group-hover:text-fg">
                →
              </span>
            </Link>
          )}

          {simple && detail && draft && (
            <section className="rounded-2xl border border-line bg-surface-elevated p-5 sm:p-6 space-y-4">
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <h2 className="text-lg font-semibold text-fg">{detail.role}</h2>
                  <span
                    className="mt-1 inline-block text-[11px] px-2 py-0.5 rounded-md bg-surface-overlay text-fg-muted font-mono"
                    title="Set by the Quality choice above, or per agent under Advanced"
                  >
                    {currentModel?.label ?? draft.model}
                    {draft.deep_reasoning && modelSupportsDeepReasoning(draft.model) ? " · deep reasoning" : ""}
                  </span>
                </div>
                <Button variant="primary" onClick={handleSave} disabled={saving || !dirty}>
                  {saving ? "Saving…" : "Save"}
                </Button>
              </div>
              {detail.name === "utility_fast" || detail.name === "research" ? (
                <p className="text-sm text-fg-muted">
                  This agent has no instructions to edit. Its model follows the Quality choice;
                  change it on its own under Advanced.
                </p>
              ) : (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className={FIELD_LABEL}>Additional instructions</span>
                    <span className="text-xs text-fg-subtle">
                      {draft.instructions.length} / {INSTRUCTIONS_MAX_CHARS} chars
                    </span>
                  </div>
                  <p className="text-[13px] text-fg-subtle mb-2 leading-relaxed">
                    Added to this agent&apos;s built-in prompt on every call, so it keeps getting
                    our prompt improvements.
                  </p>
                  <textarea
                    value={draft.instructions}
                    onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
                    maxLength={INSTRUCTIONS_MAX_CHARS}
                    rows={5}
                    placeholder="e.g. Always quote figures in EUR."
                    className={`w-full ${FIELD} py-2.5 text-[15px] resize-y leading-relaxed`}
                  />
                </div>
              )}
            </section>
          )}

          {!simple && (detail && draft ? (
            <div className="rounded-2xl border border-line bg-surface-elevated">
              <div className="p-5 sm:p-6 pb-0 sm:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h2 className="text-lg sm:text-xl font-semibold text-fg">{detail.role}</h2>
                    <p className="text-[13px] text-fg-muted mt-0.5 font-mono break-words">
                      {detail.name}
                      {detail.name === "executive"
                        ? " · orchestrator"
                        : detail.name === "utility_fast"
                        ? " · utility model knob"
                        : detail.name === "research"
                        ? " · research model knob"
                        : ` · domains: ${detail.domains.join(", ") || "—"}`}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {detail.has_override && (
                      <span className="text-[11px] uppercase tracking-widest px-2 py-1 rounded-md bg-amber-500/10 text-amber-500 border border-amber-500/20">
                        Customized
                      </span>
                    )}
                    <Button variant="primary" onClick={handleSave} disabled={saving || !dirty}>
                      {saving ? "Saving…" : "Save"}
                    </Button>
                    <OverflowMenu
                      label="More agent actions"
                      items={[
                        {
                          label: "Reset to default",
                          onSelect: () => void handleReset(),
                          disabled: resetting || !detail.has_override,
                          danger: true,
                        },
                      ]}
                    />
                  </div>
                </div>
                {dirty && (
                  <p className="mt-2 text-[13px] text-amber-500">
                    Unsaved changes. Save keeps every tab&apos;s edits.
                  </p>
                )}

                <div
                  role="tablist"
                  aria-label="Agent settings"
                  className="mt-4 -mx-5 sm:-mx-6 px-5 sm:px-6 flex gap-1 overflow-x-auto border-b border-line"
                >
                  {tabs.map((t) => {
                    const current = activeTab === t;
                    return (
                      <button
                        key={t}
                        type="button"
                        role="tab"
                        id={`council-tab-${t}`}
                        aria-selected={current}
                        aria-controls="council-tabpanel"
                        onClick={() => setTab(t)}
                        className={`flex-shrink-0 -mb-px min-h-[2.75rem] px-4 border-b-2 text-[15px] font-medium transition-colors ${
                          current
                            ? "border-accent text-fg"
                            : "border-transparent text-fg-muted hover:text-fg"
                        }`}
                      >
                        {TAB_LABEL[t]}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div
                role="tabpanel"
                id="council-tabpanel"
                aria-labelledby={`council-tab-${activeTab}`}
                className="p-5 sm:p-6 space-y-6"
              >
                {activeTab === "model" && (
                  <>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                      <label className="block">
                        <span className={FIELD_LABEL}>Role</span>
                        <input
                          type="text"
                          value={draft.role}
                          onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                          className={`w-full ${FIELD} mt-1.5 h-11 text-[15px]`}
                        />
                        {detail.role_default !== draft.role && (
                          <span className="text-xs text-fg-subtle mt-1 block">
                            Default: {detail.role_default}
                          </span>
                        )}
                      </label>

                      <div className="block">
                        <span className={FIELD_LABEL}>Model</span>
                        <div className="mt-1.5 flex gap-2">
                          <select
                            aria-label="Model provider"
                            value={currentModel?.provider ?? ""}
                            onChange={(e) => selectProvider(e.target.value)}
                            className={`${FIELD} h-11 w-2/5 min-w-0 text-[15px]`}
                          >
                            {providerGroups.map((g) => (
                              <option key={g.provider} value={g.provider}>
                                {g.label}
                              </option>
                            ))}
                          </select>
                          <select
                            aria-label="Model"
                            value={draft.model}
                            onChange={(e) => selectModel(e.target.value)}
                            className={`${FIELD} h-11 flex-1 min-w-0 text-[15px]`}
                          >
                            {pickerOptions
                              .filter((o) => o.provider === currentModel?.provider)
                              .map((o) => (
                                <option key={o.id} value={o.id}>
                                  {o.label}
                                  {o.id === detail.model_default ? " (default)" : ""}
                                </option>
                              ))}
                          </select>
                        </div>
                        {currentModel && (
                          <span className="text-xs text-fg-subtle mt-1 block font-mono break-all">
                            {currentModel.id} ·{" "}
                            {currentModel.unlisted
                              ? "not in the current allowlist"
                              : ROUTE_TEXT[currentModel.route]}
                          </span>
                        )}
                      </div>
                    </div>

                    {detail.name !== "utility_fast" && (
                      <label
                        className={`flex items-start gap-3 text-sm text-fg-muted ${
                          modelSupportsDeepReasoning(draft.model) ? "" : "opacity-60"
                        }`}
                        title={
                          modelSupportsDeepReasoning(draft.model)
                            ? "Adaptive thinking on Claude Opus/Sonnet, and on any OpenRouter model whose catalog entry supports reasoning. Ignored by models that can't reason."
                            : "Haiku doesn't support adaptive thinking — pick another model to enable deep reasoning."
                        }
                      >
                        <input
                          type="checkbox"
                          checked={draft.deep_reasoning && modelSupportsDeepReasoning(draft.model)}
                          disabled={!modelSupportsDeepReasoning(draft.model)}
                          onChange={(e) => setDraft({ ...draft, deep_reasoning: e.target.checked })}
                          className="mt-0.5 w-5 h-5 flex-shrink-0 rounded border-line-strong bg-surface accent-[rgb(var(--accent-strong))] disabled:cursor-not-allowed"
                        />
                        <span>
                          <span className="font-semibold text-fg">Deep reasoning</span> (adaptive
                          thinking — Claude Opus/Sonnet and reasoning-capable OpenRouter models; not
                          available on Haiku)
                          <span className="ml-1.5 text-xs text-fg-subtle">
                            default: {detail.deep_reasoning_default ? "on" : "off"}
                          </span>
                        </span>
                      </label>
                    )}

                    {detail.name === "utility_fast" && (
                      <p className="text-sm text-fg-muted leading-relaxed">
                        This model is used for fast, non-specialist calls: the Discord response
                        gate, Discord thread title generation, parsing human approval replies
                        (Slack/email), and disambiguating inbound messages when multiple
                        awaiting_human runs exist. Changing it has no effect on specialist
                        answers — just on these lightweight classification tasks.
                      </p>
                    )}
                    {detail.name === "research" && (
                      <p className="text-sm text-fg-muted leading-relaxed">
                        This model + deep-reasoning setting drives the executive_research
                        specialist fan-out (the periodic research scan and the manual
                        “what should we look into?” run). It applies to all specialists’
                        research turns at once and is independent of their chat models —
                        lowering it cuts research cost without touching chat quality. Per-domain
                        research focus is edited under each specialist’s Prompt tab.
                      </p>
                    )}
                  </>
                )}

                {activeTab === "instructions" && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className={FIELD_LABEL}>Additional instructions</span>
                      <span className="text-xs text-fg-subtle">
                        {draft.instructions.length} / {INSTRUCTIONS_MAX_CHARS} chars
                      </span>
                    </div>
                    <p className="text-[13px] text-fg-subtle mb-2 leading-relaxed">
                      Added after the system prompt (Prompt tab) on every call. Use this to
                      steer the agent while it keeps receiving updates to its built-in
                      prompt.
                    </p>
                    <textarea
                      value={draft.instructions}
                      onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
                      maxLength={INSTRUCTIONS_MAX_CHARS}
                      rows={8}
                      placeholder="e.g. Always quote figures in EUR. Keep answers under 200 words."
                      className={`w-full ${FIELD} py-2.5 text-[15px] resize-y leading-relaxed`}
                    />
                  </div>
                )}

                {activeTab === "prompt" && (
                  <>
                    {/* Voice Persona card — Executive only */}
                    {detail.name === "executive" && (
                      <div className="rounded-2xl border border-line bg-surface p-5 space-y-4">
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <h3 className="text-base font-semibold text-fg">Voice Persona</h3>
                            <p className="text-sm text-fg-muted mt-0.5">
                              Sets the Executive&apos;s tone and communication style. The structural prompt stays intact.
                            </p>
                          </div>
                          <Button
                            size="sm"
                            onClick={() => { setNewPersonaMode(true); setNewPersonaName(""); setNewPersonaBody(""); }}
                          >
                            + New
                          </Button>
                        </div>

                        {personaError && (
                          <p className="text-sm text-red-500">{personaError}</p>
                        )}

                        {/* Persona selector */}
                        <div>
                          <label className="block text-sm font-semibold text-fg mb-1.5">
                            Active persona
                          </label>
                          <select
                            value={draft.voice_persona_slug ?? "default"}
                            onChange={(e) => setDraft({ ...draft, voice_persona_slug: e.target.value === "default" ? null : e.target.value })}
                            className={`w-full ${FIELD} h-11 text-[15px]`}
                          >
                            {personas.filter((p) => !p.is_legacy).map(personaOption)}
                            {personas.some((p) => p.is_legacy) && (
                              <optgroup label="Legacy voices">
                                {personas.filter((p) => p.is_legacy).map(personaOption)}
                              </optgroup>
                            )}
                          </select>
                          <p className="text-[13px] text-fg-subtle mt-1">
                            Selection saves with the main Save button above.
                          </p>
                        </div>

                        {/* Persona body editor */}
                        {activePersonaDetail && (
                          <div className="space-y-2">
                            <div className="flex items-center justify-between">
                              <span className="text-sm font-semibold text-fg">
                                Persona body
                              </span>
                              <div className="flex items-center gap-2">
                                {activePersonaDetail.source_notes && (
                                  <span className="text-xs text-fg-subtle italic truncate max-w-48" title={activePersonaDetail.source_notes}>
                                    {activePersonaDetail.source_notes}
                                  </span>
                                )}
                              </div>
                            </div>
                            <input
                              type="text"
                              value={personaDisplayNameDraft}
                              onChange={(e) => setPersonaDisplayNameDraft(e.target.value)}
                              placeholder="Display name"
                              className={`w-full ${FIELD} h-11 text-[15px]`}
                            />
                            <textarea
                              value={personaBodyDraft}
                              onChange={(e) => setPersonaBodyDraft(e.target.value)}
                              rows={12}
                              className={`w-full ${FIELD} py-2.5 font-mono text-[13px] resize-y leading-relaxed`}
                            />
                            <div className="flex items-center gap-2 flex-wrap">
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={async () => {
                                  if (!activePersonaDetail) return;
                                  setSavingPersona(true);
                                  setPersonaError(null);
                                  try {
                                    const updated = await savePersona(activePersonaDetail.slug, personaDisplayNameDraft, personaBodyDraft);
                                    setActivePersonaDetail(updated);
                                    setPersonas(await listPersonas());
                                  } catch (e) {
                                    setPersonaError(e instanceof Error ? e.message : "Save failed");
                                  } finally {
                                    setSavingPersona(false);
                                  }
                                }}
                                disabled={savingPersona || !personaBodyDraft.trim()}
                              >
                                {savingPersona ? "Saving…" : "Save persona"}
                              </Button>
                              {activePersonaDetail.is_builtin && activePersonaDetail.is_customized && (
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  onClick={async () => {
                                    if (!activePersonaDetail) return;
                                    try {
                                      const restored = await resetPersona(activePersonaDetail.slug);
                                      setActivePersonaDetail(restored);
                                      setPersonaBodyDraft(restored.body);
                                      setPersonaDisplayNameDraft(restored.display_name);
                                      setPersonas(await listPersonas());
                                    } catch (e) {
                                      setPersonaError(e instanceof Error ? e.message : "Reset failed");
                                    }
                                  }}
                                >
                                  Reset to built-in
                                </Button>
                              )}
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={async () => {
                                  if (!activePersonaDetail) return;
                                  try {
                                    const duped = await savePersona(
                                      activePersonaDetail.slug + "-copy",
                                      activePersonaDetail.display_name + " (copy)",
                                      personaBodyDraft,
                                    );
                                    const updated = await listPersonas();
                                    setPersonas(updated);
                                    setDraft((d) => d ? { ...d, voice_persona_slug: duped.slug } : d);
                                  } catch (e) {
                                    setPersonaError(e instanceof Error ? e.message : "Duplicate failed");
                                  }
                                }}
                              >
                                Duplicate
                              </Button>
                              {!activePersonaDetail.is_builtin && (
                                <Button
                                  variant="danger"
                                  size="sm"
                                  onClick={async () => {
                                    if (!activePersonaDetail) return;
                                    if (!window.confirm(`Delete persona "${activePersonaDetail.display_name}"?`)) return;
                                    try {
                                      await deletePersona(activePersonaDetail.slug);
                                      const updated = await listPersonas();
                                      setPersonas(updated);
                                      setDraft((d) => d ? { ...d, voice_persona_slug: null } : d);
                                    } catch (e) {
                                      setPersonaError(e instanceof Error ? e.message : "Delete failed");
                                    }
                                  }}
                                >
                                  Delete
                                </Button>
                              )}
                            </div>
                          </div>
                        )}

                        {/* New persona inline form */}
                        {newPersonaMode && (
                          <div className="mt-2 p-4 rounded-xl border border-line-strong bg-surface-elevated space-y-3">
                            <p className="text-sm font-semibold text-fg">New persona</p>
                            <input
                              type="text"
                              value={newPersonaName}
                              onChange={(e) => setNewPersonaName(e.target.value)}
                              placeholder="Display name (e.g. Elon Musk)"
                              className={`w-full ${FIELD} h-11 text-[15px]`}
                            />
                            <textarea
                              value={newPersonaBody}
                              onChange={(e) => setNewPersonaBody(e.target.value)}
                              rows={6}
                              placeholder="Voice and style bullets — e.g. '- Direct and engineering-first...'"
                              className={`w-full ${FIELD} py-2.5 font-mono text-[13px] resize-y`}
                            />
                            <div className="flex gap-2">
                              <Button
                                variant="secondary"
                                size="sm"
                                onClick={async () => {
                                  if (!newPersonaName.trim() || !newPersonaBody.trim()) return;
                                  try {
                                    const created = await createPersona(newPersonaName, newPersonaBody);
                                    const updated = await listPersonas();
                                    setPersonas(updated);
                                    setDraft((d) => d ? { ...d, voice_persona_slug: created.slug } : d);
                                    setNewPersonaMode(false);
                                  } catch (e) {
                                    setPersonaError(e instanceof Error ? e.message : "Create failed");
                                  }
                                }}
                                disabled={!newPersonaName.trim() || !newPersonaBody.trim()}
                              >
                                Create
                              </Button>
                              <Button variant="ghost" size="sm" onClick={() => setNewPersonaMode(false)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    {detail.name !== "utility_fast" && detail.name !== "research" && (
                      <div>
                        <div className="flex items-center justify-between mb-1">
                          <span className={FIELD_LABEL}>System prompt</span>
                          <span className="text-xs text-fg-subtle">{draft.prompt.length} chars</span>
                        </div>
                        <textarea
                          value={draft.prompt}
                          onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
                          rows={20}
                          className={`w-full ${FIELD} mt-1 py-2.5 font-mono text-[13px] resize-y leading-relaxed`}
                        />
                        {draft.prompt !== detail.prompt_default && (
                          <>
                            <p className="mt-2 text-[13px] text-amber-500 leading-relaxed">
                              An edited prompt replaces the built-in one, so future updates to
                              it won&apos;t reach this agent. Additional instructions (Instructions
                              tab) don&apos;t have that cost.
                            </p>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="mt-1 -ml-3.5"
                              onClick={() => setDraft({ ...draft, prompt: detail.prompt_default })}
                            >
                              Restore default prompt in editor
                            </Button>
                          </>
                        )}
                      </div>
                    )}

                    {/* Research focus — specialists only (those with a default scope) */}
                    {detail.research_focus_default !== null && (
                      <div>
                        <div className="flex items-center justify-between mb-1">
                          <span className={FIELD_LABEL}>Research focus</span>
                          <span className="text-xs text-fg-subtle">
                            {(draft.research_focus ?? "").length} chars
                          </span>
                        </div>
                        <p className="text-[13px] text-fg-subtle mb-2 leading-relaxed">
                          The domain-scope block appended to this specialist&apos;s research
                          turn — what external signals it watches. The shared research
                          contract (output format, recency / grounding / actionability bars)
                          is fixed and not editable here.
                        </p>
                        <textarea
                          value={draft.research_focus ?? ""}
                          onChange={(e) =>
                            setDraft({ ...draft, research_focus: e.target.value })
                          }
                          rows={10}
                          className={`w-full ${FIELD} py-2.5 font-mono text-[13px] resize-y leading-relaxed`}
                        />
                        {draft.research_focus !== detail.research_focus_default && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="mt-1 -ml-3.5"
                            onClick={() =>
                              setDraft({
                                ...draft,
                                research_focus: detail.research_focus_default,
                              })
                            }
                          >
                            Restore default research focus in editor
                          </Button>
                        )}
                      </div>
                    )}
                  </>
                )}

                {activeTab === "test" && (
                  <div className="space-y-3">
                    <div>
                      <h3 className="text-base font-semibold text-fg">Test this draft</h3>
                      <p className="text-sm text-fg-muted mt-0.5">
                        Run a one-off query with the unsaved settings in these tabs. Nothing is
                        persisted.
                      </p>
                    </div>
                    <textarea
                      value={testQuery}
                      onChange={(e) => setTestQuery(e.target.value)}
                      rows={3}
                      placeholder="Ask the specialist something…"
                      className={`w-full ${FIELD} py-2.5 text-[15px]`}
                    />
                    <div className="flex flex-wrap items-center gap-3">
                      <Button variant="secondary" onClick={handleTest} disabled={testing || !testQuery.trim()}>
                        {testing ? "Running…" : "Run test"}
                      </Button>
                      {testError && <span className="text-sm text-red-500">{testError}</span>}
                    </div>
                    {testResult !== null && (
                      <div className="rounded-xl border border-line bg-surface px-4 py-3 text-[15px] text-fg whitespace-pre-wrap">
                        {testResult}
                      </div>
                    )}
                  </div>
                )}

                {activeTab === "history" && (
                  <div className="space-y-2">
                    <h3 className="text-base font-semibold text-fg">Version history</h3>
                    {history.length === 0 && (
                      <p className="text-sm text-fg-subtle">No prior versions for this agent.</p>
                    )}
                    {history.map((h) => (
                      <div
                        key={h.id}
                        className="flex items-center justify-between gap-3 rounded-xl border border-line px-4 py-3 text-sm text-fg-muted"
                      >
                        <div className="min-w-0">
                          <p className="font-mono text-xs text-fg-subtle">#{h.id} · {h.created_at}</p>
                          <p className="truncate text-fg-muted mt-0.5">
                            {[
                              h.model && `model=${h.model}`,
                              h.use_deep_reasoning !== null &&
                                `deep=${h.use_deep_reasoning ? "on" : "off"}`,
                              h.role && `role=${h.role}`,
                              h.instructions && `instructions=${h.instructions.slice(0, 60)}…`,
                              h.prompt && `prompt=${h.prompt.slice(0, 60)}…`,
                            ]
                              .filter(Boolean)
                              .join(" · ") || "(empty override)"}
                          </p>
                        </div>
                        <Button size="sm" onClick={() => handleRollback(h.id)} className="flex-shrink-0">
                          Restore
                        </Button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <p className="text-[15px] text-fg-muted">Select an agent to edit.</p>
          ))}
        </div>
      </main>
    </div>
  );
}
