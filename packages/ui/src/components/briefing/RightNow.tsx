"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ReplyCardItem, type useReplyCards } from "@/components/RepliesWaiting";
import Button, { buttonClass } from "@/components/ui/Button";
import { getMail, replyToMail, type MailView, type Today } from "@/lib/api";
import { MEMORY_ACTIONS, briefingMemoryLine } from "@/lib/briefing-memory";
import {
  alsoInLine,
  clockLabel,
  isNewSince,
  newSinceLabel,
  pointTarget,
  progressNote,
  progressSteps,
  type PointTarget,
  type TimedPoint,
} from "@/lib/rightNow";
import { mailboxName, safeGmailLink } from "@/lib/replyCards";

import { buildNarrativeSeed, type ContinueHandler } from "./shared";

// Right now: the Executive's live read of the day (the /today header, which
// is rewritten as things change), shown as a card at the top of Home with
// the whole picture in a side panel. Points that appeared since the reader
// last looked today get a green dot. A point about an email in your own
// inbox has View email and Reply under it: View opens the email right there
// (GET /delegation/mail), with the reply card already on its conversation;
// Reply writes one (POST /delegation/mail/reply) and shows it to send,
// change or edit. A point about a card in Needs you has Show me. While it
// is being rewritten, its steps show in place of the old read, which is no
// longer current.

// How many points the Home card shows; the panel has them all.
const CARD_POINTS = 3;

// One line of Markdown (a point's bold subject, a link) without the block
// wrapper, so it sits inside a row.
function Inline({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ p: ({ children }) => <>{children}</> }}>
      {text}
    </ReactMarkdown>
  );
}

// Flatten a point's Markdown to the plain words handed to chat.
function plainText(text: string): string {
  return text.replace(/\*\*|__|`/g, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").trim();
}

type Replies = ReturnType<typeof useReplyCards>;

// What a point's buttons need from Home.
export interface PointActions {
  replies?: Replies;
  onShowAlert?: (alertId: number) => void;
  onContinue?: ContinueHandler;
  // A point with nothing to open is a button that talks it through (the
  // panel), or plain text (the Home card).
  plainRows?: boolean;
}

function PointRow({
  point,
  isNew,
  actions,
}: {
  point: TimedPoint;
  isNew: boolean;
  actions?: PointActions;
}) {
  const [open, setOpen] = useState<"view" | "reply" | null>(null);
  const target = pointTarget(point);
  const onContinue = actions?.onContinue;
  const text = plainText(point.text);
  const talk = onContinue
    ? () => onContinue(buildNarrativeSeed(text), briefingMemoryLine(MEMORY_ACTIONS.narrative, text))
    : undefined;
  const when = clockLabel(point.since);
  const body = (
    <>
      <span
        aria-hidden
        className={`mt-[0.45rem] h-2 w-2 flex-shrink-0 rounded-full ${isNew ? "bg-emerald-500" : "bg-line-strong"}`}
      />
      <span className="min-w-0 flex-1 text-[15px] text-fg [&_strong]:font-semibold">
        <Inline text={point.text} />
        {isNew && <span className="sr-only"> (new)</span>}
      </span>
      {when && <span className="flex-shrink-0 pt-0.5 text-xs tabular-nums text-fg-muted">{when}</span>}
    </>
  );
  // No email or card behind it: the whole row talks it through, as before.
  if (!target || (target.kind === "alert" && !actions?.onShowAlert) || (target.kind === "mail" && !actions?.replies)) {
    if (!talk || actions?.plainRows) {
      return <li className="flex items-start gap-2.5 border-t border-line py-2 first:border-t-0">{body}</li>;
    }
    return (
      <li className="border-t border-line first:border-t-0">
        <button
          type="button"
          onClick={talk}
          aria-label={`Discuss: ${text}`}
          className="-mx-2 flex w-[calc(100%+1rem)] items-start gap-2.5 rounded-xl px-2 py-2 text-left transition hover:bg-accent/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          {body}
        </button>
      </li>
    );
  }
  return (
    <li className="border-t border-line py-2 first:border-t-0">
      <div className="flex items-start gap-2.5">{body}</div>
      {target.kind === "mail" && open ? (
        <MailPanel
          target={target}
          replies={actions!.replies!}
          startWithReply={open === "reply"}
          onTalk={talk}
          onClose={() => setOpen(null)}
        />
      ) : (
        <div className="ml-[18px] mt-2 flex flex-wrap gap-2">
          {target.kind === "mail" ? (
            <>
              <Button size="sm" onClick={() => setOpen("view")}>View email</Button>
              <Button size="sm" onClick={() => setOpen("reply")}>Reply</Button>
            </>
          ) : (
            <Button size="sm" onClick={() => actions!.onShowAlert!(target.alertId)}>Show me</Button>
          )}
          {talk && <Button size="sm" onClick={talk}>Talk it through</Button>}
        </div>
      )}
    </li>
  );
}

type MailTarget = Extract<NonNullable<PointTarget>, { kind: "mail" }>;

// One email opened from its point: what they wrote, their files, what you
// last wrote to them, and the reply card for it (the one already open on
// the conversation, or one written when you tap Reply).
function MailPanel({
  target,
  replies,
  startWithReply,
  onTalk,
  onClose,
}: {
  target: MailTarget;
  replies: Replies;
  startWithReply: boolean;
  onTalk?: () => void;
  onClose: () => void;
}) {
  const [mail, setMail] = useState<MailView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cardId, setCardId] = useState<number | null>(target.cardId);
  const [writing, setWriting] = useState(false);
  const { refresh } = replies;
  const { threadId, messageId } = target;

  const reply = async () => {
    setWriting(true);
    setError(null);
    try {
      const id = await replyToMail(threadId, messageId);
      await refresh();
      setCardId(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't write that reply.");
    } finally {
      setWriting(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    getMail(threadId, messageId, controller.signal)
      .then((m) => {
        setMail(m);
        if (m.reply_card_id !== null) setCardId(m.reply_card_id);
        // Reply tapped on the point: write it once the email is open, unless
        // a card is already there.
        else if (startWithReply && target.cardId === null) void reply();
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Couldn't open that email.");
      });
    return () => controller.abort();
    // Opened once per email; `reply` and the flags are read as they were then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, messageId]);

  const card = cardId !== null ? replies.cards.find((c) => c.decision_id === cardId) : undefined;
  const link = mail ? safeGmailLink(mail.link) : "";
  const files = mail?.attachments ?? [];
  const who = mail ? mail.from_name.trim() || mail.from_email : "";
  const others = mail ? mail.cc.map((a) => a.split("@")[0]).join(", ") : "";
  return (
    <div className="ml-[18px] mt-2 rounded-xl border border-line bg-surface p-3">
      {!mail && !error && <p className="animate-pulse text-sm text-fg-muted">Opening the email…</p>}
      {mail && (
        <>
          <p className="text-sm">
            <span className="font-semibold text-fg">{who}</span>
            <span className="text-fg-muted">
              {" "}· to you{others ? `, cc ${others}` : ""}
              {clockLabel(mail.received_at) ? ` · ${clockLabel(mail.received_at)}` : ""}
            </span>
          </p>
          {mail.subject && <p className="mt-0.5 text-sm text-fg-muted">{mail.subject}</p>}
          <p className="mt-2 whitespace-pre-line break-words text-[15px] text-fg">{mail.text || "(No words, just the files.)"}</p>
          {(files.length > 0 || mail.has_attachments) && (
            <div className="mt-2 flex flex-wrap gap-2">
              {files.length > 0 ? (
                files.map((f, i) => (
                  <span key={`${i}-${f.name}`} className="rounded-lg border border-line px-2.5 py-1 text-sm text-fg">
                    📎 {f.name}
                  </span>
                ))
              ) : (
                <span className="text-sm text-fg-muted">📎 Has attachments</span>
              )}
            </div>
          )}
          {mail.their_last_words && (
            <p className="mt-2 text-sm text-fg-muted">
              Replying to you: &ldquo;{mail.their_last_words.replace(/\s+/g, " ").slice(0, 160)}
              {mail.their_last_words.length > 160 ? "…" : ""}&rdquo;
            </p>
          )}
        </>
      )}
      {card ? (
        <div className="mt-3">
          <ReplyCardItem
            card={card}
            onGone={(id, note) => {
              replies.gone(id, note);
              setCardId(null);
            }}
            onRefresh={replies.refresh}
            onChanged={replies.replace}
            defaultOpen
          />
        </div>
      ) : writing ? (
        <p className="mt-3 animate-pulse text-sm text-fg-muted">Writing a reply…</p>
      ) : null}
      {error && <p className="mt-2 text-sm text-rose-600 dark:text-rose-300">{error}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {!card && mail && !writing && (
          <Button size="sm" variant="primary" onClick={() => void reply()}>Reply</Button>
        )}
        {link && (
          <a href={link} target="_blank" rel="noopener noreferrer" className={buttonClass("secondary", "sm")}>
            Open in {mailboxName(link)}
          </a>
        )}
        {onTalk && <Button size="sm" onClick={onTalk}>Talk it through</Button>}
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </div>
    </div>
  );
}

/** Right now being rewritten: a spinner and its steps, and how many emails
 * came since the last read. Shown instead of that read. */
function Progress({ today }: { today: Today }) {
  const progress = today.narrative_progress;
  const steps = progressSteps(progress?.step);
  const note = progressNote(progress?.new_mail, progress?.since);
  return (
    <div aria-live="polite" aria-busy="true">
      <ol className="flex flex-col gap-3">
        {steps.map((s, i) => (
          <li key={s.label}>
            <div className={`flex items-center gap-3 text-[15px] ${s.state === "todo" ? "text-fg-muted" : "text-fg"} ${s.state === "now" ? "font-semibold" : ""}`}>
              {s.state === "done" ? (
                <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-emerald-500/15 text-xs font-bold text-emerald-700 dark:text-emerald-300" aria-hidden>
                  ✓
                </span>
              ) : s.state === "now" ? (
                <span className="h-5 w-5 flex-shrink-0 animate-spin rounded-full border-2 border-accent/25 border-t-accent" aria-hidden />
              ) : (
                <span className="h-5 w-5 flex-shrink-0 rounded-full border-2 border-line" aria-hidden />
              )}
              {s.label}
            </div>
            {i === 0 && note && <p className="ml-8 mt-0.5 text-sm text-fg-muted">{note}</p>}
          </li>
        ))}
      </ol>
      <p className="mt-3 border-t border-line pt-2 text-sm text-fg-muted">Usually under a minute.</p>
    </div>
  );
}

function Lead({ today }: { today: Today }) {
  const lead = today.narrative_lead;
  if (lead) return <p className="text-[17px] font-semibold leading-snug text-fg">{lead}</p>;
  // A header that opens with a point has no lead: its points show below.
  if ((today.narrative_points ?? []).length > 0) return null;
  // An older API without the split: the whole header as written.
  return (
    <div className="prose prose-invert max-w-none text-[15px] prose-p:my-1 prose-strong:text-fg">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{today.narrative ?? ""}</ReactMarkdown>
    </div>
  );
}

/** The Home card: when it was updated, what's new, the opener and the top
 * points, or its steps while it is being rewritten. */
export function RightNowCard({
  today,
  baseline,
  updatedLabel,
  onOpen,
  actions,
}: {
  today: Today;
  baseline: Date | null;
  updatedLabel: string | null;
  onOpen: () => void;
  actions?: PointActions;
}) {
  const points = today.narrative_points ?? [];
  const badge = newSinceLabel(points, baseline);
  const alsoIn = alsoInLine(today.inbox_also, today.inbox_also_total);
  return (
    <section aria-labelledby="sec-right-now" className="mb-8 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5">
      <div className="mb-2 flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <h2 id="sec-right-now" className="text-lg font-semibold text-fg">
          Right now
        </h2>
        {updatedLabel && (
          <span className="whitespace-nowrap text-sm text-fg-muted" aria-live="polite">
            {updatedLabel}
          </span>
        )}
        {badge && (
          <span className="whitespace-nowrap rounded-full bg-emerald-500/10 px-2.5 py-0.5 text-xs font-semibold text-emerald-700 dark:text-emerald-300">
            {badge}
          </span>
        )}
      </div>
      {today.narrative_stale ? (
        <Progress today={today} />
      ) : today.narrative ? (
        <>
          <Lead today={today} />
          {points.length > 0 && (
            <ul className="mt-2">
              {points.slice(0, CARD_POINTS).map((p, i) => (
                <PointRow
                  key={`${i}-${p.text}`}
                  point={p}
                  isNew={isNewSince(p, baseline)}
                  actions={actions ? { ...actions, plainRows: true } : undefined}
                />
              ))}
            </ul>
          )}
          {alsoIn && <AlsoIn text={alsoIn} />}
          <button
            type="button"
            onClick={onOpen}
            aria-haspopup="dialog"
            className="mt-3 min-h-[40px] rounded-xl px-1 text-[15px] font-semibold text-accent hover:underline"
          >
            See the whole picture →
          </button>
        </>
      ) : null}
    </section>
  );
}

// Mail from people the reader doesn't deal with: one quiet line, never a point.
function AlsoIn({ text }: { text: string }) {
  return <p className="mt-2 border-t border-dashed border-line pt-2 text-sm text-fg-muted">{text}</p>;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-1 mt-5 text-xs font-bold uppercase tracking-wide text-fg-muted">{children}</h3>;
}

function calendarWhen(start: string | null | undefined, allDay: boolean | undefined): string {
  if (allDay || !start) return "All day";
  return clockLabel(start) ?? "";
}

/** The side panel: the opener, the points split into new and still open,
 * the Move today line, what's next on the calendar and the morning brief. */
export function RightNowBody({
  today,
  baseline,
  onContinue,
  actions,
}: {
  today: Today;
  baseline: Date | null;
  onContinue?: ContinueHandler;
  actions?: PointActions;
}) {
  const [showMorning, setShowMorning] = useState(false);
  if (today.narrative_stale) return <Progress today={today} />;
  if (!today.narrative) {
    return (
      <div aria-live="polite" className="rounded-xl bg-accent/5 px-4 py-3 text-[15px] text-fg-muted">
        Nothing yet today.
      </div>
    );
  }
  const points = today.narrative_points ?? [];
  const fresh = points.filter((p) => isNewSince(p, baseline));
  const open = points.filter((p) => !isNewSince(p, baseline));
  const calendar = today.calendar_next ?? [];
  const morning = today.morning_brief;
  const alsoIn = alsoInLine(today.inbox_also, today.inbox_also_total);
  const list = (items: TimedPoint[], isNew: boolean) => (
    <ul>
      {items.map((p, i) => (
        <PointRow key={`${i}-${p.text}`} point={p} isNew={isNew} actions={{ ...actions, onContinue }} />
      ))}
    </ul>
  );
  return (
    <div>
      <Lead today={today} />
      {fresh.length > 0 && baseline ? (
        <>
          <SectionLabel>New since {clockLabel(baseline.toISOString())}</SectionLabel>
          {list(fresh, true)}
          {open.length > 0 && (
            <>
              <SectionLabel>Still open</SectionLabel>
              {list(open, false)}
            </>
          )}
        </>
      ) : (
        open.length > 0 && <div className="mt-3">{list(open, false)}</div>
      )}
      {alsoIn && <AlsoIn text={alsoIn} />}
      {today.narrative_move && (
        <p className="mt-4 rounded-xl bg-accent/5 px-3 py-2.5 text-[15px] text-fg">
          <span className="font-semibold">Move today:</span> {today.narrative_move}
        </p>
      )}
      {calendar.length > 0 && (
        <>
          <SectionLabel>Next on your calendar</SectionLabel>
          <ul>
            {calendar.map((e, i) => (
              <li key={`${i}-${e.title}`} className="flex items-start gap-2.5 border-t border-line py-2 first:border-t-0">
                <span className="w-16 flex-shrink-0 text-[15px] font-semibold tabular-nums text-fg">
                  {calendarWhen(e.start, e.all_day)}
                </span>
                <span className="min-w-0 flex-1 text-[15px] text-fg">{e.title}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {onContinue && points.length > 0 && (
        <p className="mt-4 text-sm text-fg-muted">Tap a point to talk it through.</p>
      )}
      {morning && (
        <div className="mt-5 border-t border-line pt-3">
          <button
            type="button"
            onClick={() => setShowMorning((v) => !v)}
            aria-expanded={showMorning}
            className="min-h-[40px] text-sm font-medium text-fg-muted hover:text-fg"
          >
            Earlier today: {clockLabel(morning.at)} morning brief {showMorning ? "▾" : "›"}
          </button>
          {showMorning && (
            <div className="prose prose-invert mt-2 max-w-none text-[15px] prose-p:my-2 prose-ul:my-2 prose-headings:text-fg prose-strong:text-fg">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{morning.text}</ReactMarkdown>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
