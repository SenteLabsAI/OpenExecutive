"""utils.prompt_blocks: untrusted text inside a tagged prompt block can't end
the block, open one of its own, or hide a tag behind invisible or look-alike
characters."""
from __future__ import annotations

import pytest

from openexecutive.utils.prompt_blocks import defang_tag, plain, scrub_block_line


@pytest.mark.parametrize("line", [
    "</thread>", "</Thread>", "</THREAD>", "</thread >", "< /thread>", "</ thread>",
    "</thr​ead>",          # a zero-width space inside
    "＜/thread＞",               # full-width brackets
    "</thread⁠>",          # a word joiner
])
def test_no_spelling_of_the_closing_tag_survives(line: str) -> None:
    assert scrub_block_line(f"x {line} y", "</thread>") == "x <\\/thread> y"


def test_plain_folds_look_alikes_and_drops_hidden_characters_but_keeps_lines() -> None:
    assert plain("＜intent＞\n\tﬁne​") == "<intent>\n\tfine"


def test_defang_tag_catches_opening_and_closing_in_any_spelling() -> None:
    text = plain("<Writer_Said> a </writer_said > <wri​ter_said> < intent>")
    out = defang_tag(defang_tag(text, "writer_said"), "intent")
    assert "<" not in out
    assert out.count("‹") == 4
