"use client";

import { useCallback, useEffect, useState } from "react";

import {
  AgentDetail,
  AgentHistoryEntry,
  AgentMeta,
  Persona,
  PersonaMeta,
  createPersona,
  deletePersona,
  getAgentDetail,
  getPersona,
  listAgentHistory,
  listAgentModels,
  listAgents,
  listPersonas,
  patchAgent,
  resetAgent,
  resetPersona,
  rollbackAgent,
  savePersona,
  testAgent,
} from "@/lib/api";

interface DraftState {
  role: string;
  model: string;
  deep_reasoning: boolean;
  prompt: string;
  voice_persona_slug: string | null;
  research_focus: string | null;
}

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
    (d.research_focus ?? null) !== draft.research_focus
  );
}

export default function CouncilPage() {
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [history, setHistory] = useState<AgentHistoryEntry[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);

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
      setHistoryOpen(false);
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
    if (selected) loadDetail(selected);
    // Refetch the model allowlist when the agent changes (every agent
    // currently gets the same list).
    listAgentModels(selected ?? undefined).then(setModels).catch(() => {});
  }, [selected, loadDetail]);

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
    <div className="flex flex-1 min-h-0 bg-surface text-fg overflow-hidden">
      <aside className="w-64 flex-shrink-0 border-r border-line flex flex-col bg-surface-elevated">
        <div className="px-3 py-4 overflow-y-auto">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-widest text-fg-subtle mb-2">
            Agent Council
          </p>
          <nav className="space-y-0.5">
            {agents.map((a) => (
              <button
                key={a.name}
                onClick={() => setSelected(a.name)}
                className={`w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-lg text-xs transition-colors ${
                  selected === a.name
                    ? "bg-indigo-500/10 text-indigo-300"
                    : "text-fg-muted hover:text-fg hover:bg-surface-overlay/60"
                }`}
              >
                <span
                  className={`mt-1 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    a.has_override ? "bg-amber-400" : "bg-surface-input"
                  }`}
                  title={a.has_override ? "Has override" : "Default config"}
                />
                <span className="flex-1 min-w-0">
                  <span className="block font-medium text-fg uppercase tracking-wide text-[10px]">
                    {a.name}
                  </span>
                  <span className="block truncate text-fg-muted">{a.role}</span>
                </span>
              </button>
            ))}
          </nav>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-8 py-10 space-y-6">
          <div>
            <h1 className="text-2xl font-bold text-fg">Agent Council</h1>
            <p className="mt-2 text-sm text-fg-muted">
              Edit each specialist&apos;s prompt, model, and behavior. Changes apply on the next
              specialist call — no restart needed. Resetting restores the built-in defaults.
            </p>
          </div>

          {error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}

          {detail && draft ? (
            <div className="space-y-6">
              <div className="rounded-xl border border-line bg-surface px-6 py-5 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-lg font-semibold text-fg">
                      {detail.role}
                    </h2>
                    <p className="text-xs text-fg-muted mt-0.5 font-mono">
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
                      <span className="text-[10px] uppercase tracking-widest px-2 py-1 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                        Customized
                      </span>
                    )}
                    <button
                      onClick={handleReset}
                      disabled={resetting || !detail.has_override}
                      className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-fg-muted hover:border-red-500/40 hover:text-red-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      Reset to default
                    </button>
                    <button
                      onClick={handleSave}
                      disabled={saving || !dirty}
                      className="text-xs px-3 py-1.5 rounded-lg bg-indigo-500/20 border border-indigo-500/30 text-indigo-300 hover:bg-indigo-500/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {saving ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <label className="block text-xs">
                    <span className="text-fg-muted uppercase tracking-widest text-[10px] font-semibold">
                      Role
                    </span>
                    <input
                      type="text"
                      value={draft.role}
                      onChange={(e) => setDraft({ ...draft, role: e.target.value })}
                      className="mt-1 w-full px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none text-sm"
                    />
                    {detail.role_default !== draft.role && (
                      <span className="text-[10px] text-fg-subtle mt-1 block">
                        Default: {detail.role_default}
                      </span>
                    )}
                  </label>

                  <label className="block text-xs">
                    <span className="text-fg-muted uppercase tracking-widest text-[10px] font-semibold">
                      Model
                    </span>
                    <select
                      value={draft.model}
                      onChange={(e) => {
                        const model = e.target.value;
                        setDraft({
                          ...draft,
                          model,
                          deep_reasoning: modelSupportsDeepReasoning(model)
                            ? draft.deep_reasoning
                            : false,
                        });
                      }}
                      className="mt-1 w-full px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none text-sm"
                    >
                      {Array.from(new Set([detail.model_default, draft.model, ...models])).map(
                        (m) => (
                          <option key={m} value={m}>
                            {m}
                            {m === detail.model_default ? " (default)" : ""}
                          </option>
                        )
                      )}
                    </select>
                  </label>
                </div>

                {detail.name !== "utility_fast" && (
                  <label
                    className={`flex items-center gap-2 text-xs text-fg-muted ${
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
                      className="rounded border-line-strong bg-surface disabled:cursor-not-allowed"
                    />
                    Deep reasoning (adaptive thinking — Claude Opus/Sonnet and reasoning-capable
                    OpenRouter models; not available on Haiku)
                    <span className="text-[10px] text-fg-subtle">
                      default: {detail.deep_reasoning_default ? "on" : "off"}
                    </span>
                  </label>
                )}

                {detail.name === "utility_fast" ? (
                  <p className="text-xs text-fg-muted leading-relaxed">
                    This model is used for fast, non-specialist calls: the Discord response
                    gate, Discord thread title generation, parsing human approval replies
                    (Slack/email), and disambiguating inbound messages when multiple
                    awaiting_human runs exist. Changing it has no effect on specialist
                    answers — just on these lightweight classification tasks.
                  </p>
                ) : detail.name === "research" ? (
                  <p className="text-xs text-fg-muted leading-relaxed">
                    This model + deep-reasoning setting drives the executive_research
                    specialist fan-out (the periodic research scan and the manual
                    “what should we look into?” run). It applies to all specialists’
                    research turns at once and is independent of their chat models —
                    lowering it cuts research cost without touching chat quality. Per-domain
                    research focus is edited under each specialist below.
                  </p>
                ) : (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-fg-muted uppercase tracking-widest text-[10px] font-semibold">
                        System prompt
                      </span>
                      <span className="text-[10px] text-fg-subtle">
                        {draft.prompt.length} chars
                      </span>
                    </div>
                    <textarea
                      value={draft.prompt}
                      onChange={(e) => setDraft({ ...draft, prompt: e.target.value })}
                      rows={20}
                      className="w-full font-mono text-xs px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none resize-y leading-relaxed"
                    />
                    {draft.prompt !== detail.prompt_default && (
                      <button
                        onClick={() => setDraft({ ...draft, prompt: detail.prompt_default })}
                        className="mt-2 text-[10px] text-fg-muted hover:text-fg underline"
                      >
                        Restore default prompt in editor
                      </button>
                    )}
                  </div>
                )}

                {/* Research focus — specialists only (those with a default scope) */}
                {detail.research_focus_default !== null && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-fg-muted uppercase tracking-widest text-[10px] font-semibold">
                        Research focus
                      </span>
                      <span className="text-[10px] text-fg-subtle">
                        {(draft.research_focus ?? "").length} chars
                      </span>
                    </div>
                    <p className="text-[10px] text-fg-subtle mb-1 leading-relaxed">
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
                      className="w-full font-mono text-xs px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none resize-y leading-relaxed"
                    />
                    {draft.research_focus !== detail.research_focus_default && (
                      <button
                        onClick={() =>
                          setDraft({
                            ...draft,
                            research_focus: detail.research_focus_default,
                          })
                        }
                        className="mt-2 text-[10px] text-fg-muted hover:text-fg underline"
                      >
                        Restore default research focus in editor
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* Voice Persona card — Executive only */}
              {detail.name === "executive" && (
                <div className="rounded-xl border border-line bg-surface px-6 py-5 space-y-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="text-sm font-semibold text-fg">Voice Persona</h3>
                      <p className="text-xs text-fg-muted mt-0.5">
                        Sets the Executive&apos;s tone and communication style. The structural prompt stays intact.
                      </p>
                    </div>
                    <button
                      onClick={() => { setNewPersonaMode(true); setNewPersonaName(""); setNewPersonaBody(""); }}
                      className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-fg-muted hover:border-indigo-500/40 hover:text-indigo-300 transition-colors"
                    >
                      + New
                    </button>
                  </div>

                  {personaError && (
                    <p className="text-xs text-red-400">{personaError}</p>
                  )}

                  {/* Persona selector */}
                  <div>
                    <label className="block text-[10px] font-semibold uppercase tracking-widest text-fg-muted mb-1">
                      Active persona
                    </label>
                    <select
                      value={draft.voice_persona_slug ?? "default"}
                      onChange={(e) => setDraft({ ...draft, voice_persona_slug: e.target.value === "default" ? null : e.target.value })}
                      className="w-full px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none text-sm"
                    >
                      {personas.map((p) => (
                        <option key={p.slug} value={p.slug}>
                          {p.display_name}
                          {p.is_builtin && !p.is_customized ? " · built-in" : ""}
                          {p.is_customized ? " · customized" : ""}
                          {!p.is_builtin ? " · custom" : ""}
                        </option>
                      ))}
                    </select>
                    <p className="text-[10px] text-fg-subtle mt-1">
                      Selection saves with the main Save button above.
                    </p>
                  </div>

                  {/* Persona body editor */}
                  {activePersonaDetail && (
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] font-semibold uppercase tracking-widest text-fg-muted">
                          Persona body
                        </span>
                        <div className="flex items-center gap-2">
                          {activePersonaDetail.source_notes && (
                            <span className="text-[10px] text-fg-subtle italic truncate max-w-48" title={activePersonaDetail.source_notes}>
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
                        className="w-full px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none text-sm"
                      />
                      <textarea
                        value={personaBodyDraft}
                        onChange={(e) => setPersonaBodyDraft(e.target.value)}
                        rows={12}
                        className="w-full font-mono text-xs px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none resize-y leading-relaxed"
                      />
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
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
                          className="text-xs px-3 py-1.5 rounded-lg bg-indigo-500/20 border border-indigo-500/30 text-indigo-300 hover:bg-indigo-500/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          {savingPersona ? "Saving…" : "Save persona"}
                        </button>
                        {activePersonaDetail.is_builtin && activePersonaDetail.is_customized && (
                          <button
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
                            className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-fg-muted hover:border-amber-500/40 hover:text-amber-400 transition-colors"
                          >
                            Reset to built-in
                          </button>
                        )}
                        <button
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
                          className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-fg-muted hover:border-fg-muted hover:text-fg transition-colors"
                        >
                          Duplicate
                        </button>
                        {!activePersonaDetail.is_builtin && (
                          <button
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
                            className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-red-400 hover:border-red-500/40 hover:bg-red-500/10 transition-colors"
                          >
                            Delete
                          </button>
                        )}
                      </div>
                    </div>
                  )}

                  {/* New persona inline form */}
                  {newPersonaMode && (
                    <div className="mt-2 p-4 rounded-lg border border-line-strong bg-surface space-y-3">
                      <p className="text-xs font-semibold text-fg">New persona</p>
                      <input
                        type="text"
                        value={newPersonaName}
                        onChange={(e) => setNewPersonaName(e.target.value)}
                        placeholder="Display name (e.g. Elon Musk)"
                        className="w-full px-3 py-2 rounded-lg bg-surface-elevated border border-line-strong text-fg text-sm focus:outline-none focus:border-indigo-500/40"
                      />
                      <textarea
                        value={newPersonaBody}
                        onChange={(e) => setNewPersonaBody(e.target.value)}
                        rows={6}
                        placeholder="Voice and style bullets — e.g. '- Direct and engineering-first...'"
                        className="w-full font-mono text-xs px-3 py-2 rounded-lg bg-surface-elevated border border-line-strong text-fg focus:outline-none focus:border-indigo-500/40 resize-y"
                      />
                      <div className="flex gap-2">
                        <button
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
                          className="text-xs px-3 py-1.5 rounded-lg bg-indigo-500/20 border border-indigo-500/30 text-indigo-300 hover:bg-indigo-500/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          Create
                        </button>
                        <button
                          onClick={() => setNewPersonaMode(false)}
                          className="text-xs px-3 py-1.5 rounded-lg border border-line-strong text-fg-muted hover:text-fg transition-colors"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {detail.name !== "utility_fast" && (
              <div className="rounded-xl border border-line bg-surface px-6 py-5 space-y-3">
                <div>
                  <h3 className="text-sm font-semibold text-fg">Test this draft</h3>
                  <p className="text-xs text-fg-muted mt-0.5">
                    Run a one-off query with the unsaved settings above. Nothing is persisted.
                  </p>
                </div>
                <textarea
                  value={testQuery}
                  onChange={(e) => setTestQuery(e.target.value)}
                  rows={3}
                  placeholder="Ask the specialist something…"
                  className="w-full text-sm px-3 py-2 rounded-lg bg-surface border border-line text-fg focus:border-indigo-500/40 focus:outline-none"
                />
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleTest}
                    disabled={testing || !testQuery.trim()}
                    className="text-xs px-3 py-1.5 rounded-lg bg-violet-500/20 border border-violet-500/30 text-violet-300 hover:bg-violet-500/30 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {testing ? "Running…" : "Run test"}
                  </button>
                  {testError && <span className="text-xs text-red-400">{testError}</span>}
                </div>
                {testResult !== null && (
                  <div className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-fg whitespace-pre-wrap">
                    {testResult}
                  </div>
                )}
              </div>
              )}

              <div className="rounded-xl border border-line bg-surface px-6 py-5">
                <button
                  onClick={() => setHistoryOpen((o) => !o)}
                  className="flex items-center justify-between w-full text-sm font-semibold text-fg"
                >
                  <span>Version history</span>
                  <span className="text-xs text-fg-muted">{historyOpen ? "Hide" : "Show"}</span>
                </button>
                {historyOpen && (
                  <div className="mt-3 space-y-2 max-h-80 overflow-y-auto">
                    {history.length === 0 && (
                      <p className="text-xs text-fg-subtle">No prior versions for this agent.</p>
                    )}
                    {history.map((h) => (
                      <div
                        key={h.id}
                        className="flex items-center justify-between rounded-lg border border-line px-3 py-2 text-xs text-fg-muted"
                      >
                        <div className="min-w-0">
                          <p className="font-mono text-[10px] text-fg-subtle">#{h.id} · {h.created_at}</p>
                          <p className="truncate text-fg-muted mt-0.5">
                            {[
                              h.model && `model=${h.model}`,
                              h.use_deep_reasoning !== null &&
                                `deep=${h.use_deep_reasoning ? "on" : "off"}`,
                              h.role && `role=${h.role}`,
                              h.prompt && `prompt=${h.prompt.slice(0, 60)}…`,
                            ]
                              .filter(Boolean)
                              .join(" · ") || "(empty override)"}
                          </p>
                        </div>
                        <button
                          onClick={() => handleRollback(h.id)}
                          className="text-[10px] px-2 py-1 rounded border border-line-strong text-fg hover:bg-surface-overlay hover:text-fg flex-shrink-0"
                        >
                          Restore
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <p className="text-sm text-fg-muted">Select an agent to edit.</p>
          )}
        </div>
      </main>
    </div>
  );
}
