"""The asker's own mailbox for an Act as me eval scenario (``delegation:``).

A scenario's threads, served the way ``delegation.gmail.DelegateGmail`` serves
a real mailbox (searches, thread and message reads, the inbox and sent mail),
and every draft the turn saves kept here for the judge — nothing reaches
Google. Built fresh per run by ``scenarios.scenario_delegation``.
"""
from __future__ import annotations

from datetime import datetime

from openexecutive.delegation.gmail import (
    CreatedDraft,
    DraftSpec,
    GmailError,
    MailAttachment,
    MailMessage,
    MailThread,
    ThreadSummary,
)


class ScenarioMailbox:
    def __init__(
        self, email: str, threads: list[MailThread], files: dict[tuple[str, int], bytes] | None = None
    ) -> None:
        self.email = email
        self.threads = {t.id: t for t in threads}
        # Attached files' contents by (message id, 1-based index).
        self.files = files or {}
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

    async def get_message(self, message_id: str) -> MailMessage:
        for thread in self.threads.values():
            for message in thread.messages:
                if message.id == message_id:
                    return message
        raise GmailError("no such message")

    async def list_attachments(self, message_id: str) -> list[MailAttachment]:
        return list((await self.get_message(message_id)).attachments)

    async def attachment_bytes(self, message_id: str, index: int) -> tuple[MailAttachment, bytes]:
        attached = await self.list_attachments(message_id)
        if not 1 <= index <= len(attached) or (message_id, index) not in self.files:
            raise GmailError("no such attachment")
        return attached[index - 1], self.files[(message_id, index)]

    async def inbox_message_ids(self, *, after: datetime, max_results: int = 25) -> list[tuple[str, str]]:
        """Each thread's newest message from someone else (scenario mail has
        no received time, so ``after`` lets everything through)."""
        out = []
        for thread in self.threads.values():
            inbound = [m for m in thread.messages if "SENT" not in m.labels]
            if inbound:
                out.append((inbound[-1].id, thread.id))
        return out[:max_results]

    async def send_as_addresses(self) -> list[str]:
        return [self.email]

    async def list_sent(self, limit: int = 40) -> list[MailMessage]:
        sent = [m for t in self.threads.values() for m in t.messages if "SENT" in m.labels]
        return sent[::-1][:limit]

    async def create_draft(self, spec: DraftSpec) -> CreatedDraft:
        self.drafts.append(spec)
        n = len(self.drafts)
        return CreatedDraft(draft_id=f"eval-draft-{n}", message_id=f"eval{n}", thread_id=spec.thread_id or f"new{n}")
