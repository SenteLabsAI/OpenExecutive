"""Act as me: the Executive writing *as* a person, from their own mailbox.

Everywhere else the Executive speaks as itself, from its own account. This
package is the one place it may write as someone else — the person it is
speaking with, when that person turned it on for themselves — and only into
that person's own Gmail:

- ``settings``    who has it on, and the per-turn decision to offer it
- ``gmail``       a direct Gmail client for that person's mailbox, outside the
                  MCP gateway, so the model can never reach it untyped
- ``voice``       "How I write": a style profile learned from their sent mail
- ``ghostwriter`` the tool-less call that writes a draft in that voice

Phase 1 only drafts: nothing here can send, and only the principal may turn it
on (``settings.can_delegate``). See ``architecture/prebuilt/delegation.json``.
"""
