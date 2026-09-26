"""The asker's own mailbox for an Act as me eval scenario (``delegation:``).

A scenario's threads, served the way ``delegation.gmail.DelegateGmail`` serves
a real mailbox, and every draft the turn saves kept here for the judge —
nothing reaches Google. Built fresh per run by ``scenarios.scenario_delegation``.
"""
from __future__ import annotations

from openexecutive.delegation.gmail import (
    CreatedDraft,
    DraftSpec,
    GmailError,
    MailThread,
    ThreadSummary,
)


class ScenarioMailbox:
    def __init__(self, email: str, threads: list[MailThread]) -> None:
        self.email = email
        self.threads = {t.id: t for t in threads}
        self.drafts: list[DraftSpec] = []

    async def profile_email(self) -> str:
        return self.email

    async def search_threads(self, query: str, *, max_results: int = 5) -> list[ThreadSummary]:
        out = []
        for thread in list(self.threads.values())[:max_results]:
            last = thread.messages[-1] if thread.messages else None
            out.append(ThreadSummary(
                id=thread.id,
                subject=last.subject if last else "",
                sender=(f"{last.from_name} <{last.from_addr}>" if last else ""),
                date=last.date if last else "",
            ))
        return out

    async def get_thread(self, thread_id: str) -> MailThread:
        if thread_id not in self.threads:
            raise GmailError("no such thread")
        return self.threads[thread_id]

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        self.drafts.append(spec)
        n = len(self.drafts)
        return CreatedDraft(draft_id=f"eval-draft-{n}", message_id=f"eval{n}", thread_id=spec.thread_id or f"new{n}")
