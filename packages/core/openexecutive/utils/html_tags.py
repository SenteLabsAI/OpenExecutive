"""One tag strip, shared by everything that reduces someone else's HTML to text.

``monitoring.sources.page_watch`` reads pages a watched site's server shapes,
and ``delegation.gmail`` reads mail anyone can send. Both run on the event
loop, so both drop tags with this forward scan rather than
``re.sub(r"<[^>]+>", ...)``: on text with many "<" and no ">", that pattern runs
from each "<" to the end of the text, fails, and starts over at the next "<" —
quadratic, about 40 minutes of CPU for a 2 MB page.
"""
from __future__ import annotations


def strip_tags(text: str, *, repl: str = "") -> str:
    """``text`` with each tag — a "<" through the next ">", with something
    between — replaced by ``repl``, in one forward pass.

    Gives the same result as ``re.sub(r"<[^>]+>", repl, text)``: ``<>`` is not
    a tag, and a "<" that nothing after it closes stays as text.
    """
    out: list[str] = []
    at = 0
    while (lt := text.find("<", at)) != -1:
        gt = text.find(">", lt + 1)
        if gt == -1:
            break  # nothing after this closes a tag
        out.append(text[at:gt + 1] if gt == lt + 1 else text[at:lt] + repl)
        at = gt + 1
    out.append(text[at:])
    return "".join(out)
