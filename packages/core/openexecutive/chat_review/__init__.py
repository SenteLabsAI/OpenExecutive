"""Reflection, looking back: the Executive reads yesterday's conversations
and proposes what it could do better.

- ``store`` — the tables (runs, findings, lessons, the owner's switches).
- ``pick`` — which conversations a run reads (SQL, no model).
- ``review`` — one model call per conversation; findings checked in code.
- ``lessons`` — the approved lessons and the ``<lessons>`` block every chat
  turn carries.
- ``run`` — the pass itself, the second step of the daily reflection.

Nothing here notifies anyone: the owner opens Pulse → Reflection when they
choose, and nothing changes until they approve it there.
"""
