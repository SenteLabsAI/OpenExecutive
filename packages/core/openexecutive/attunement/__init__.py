"""Attunement — the Executive adapting to each person it works with.

Built on one prerequisite: knowing who actually said what. ``chat_messages``
records the resolved rostered sender of every user message
(``sender_person_id``) and explicit 👍/👎 on replies (``feedback``), so nothing
here ever reads an outsider's words as someone on the roster.

- :mod:`openexecutive.attunement.open_loops` — commitments and asks from anyone
  on the roster become open loops the nudge engine chases once due, and close
  when the owner says they're done.
- :mod:`openexecutive.attunement.outcomes` — every proactive DM to a person is
  resolved replied / acted / void / ignored, and the rates steer which
  outreach the nudge engine and the morning standup send to whom.
- :mod:`openexecutive.attunement.style` — a few short working-style rules per
  person, learned from their own reactions and requests and pinned into their
  own turns; editable and lockable on the person page.
"""
