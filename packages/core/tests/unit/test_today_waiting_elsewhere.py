"""Home's Needs you rows for what waits on other pages (GET /today
``waiting_elsewhere``): the Watch list's suggestions, so Home never calls
the day quiet while they wait."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from openexecutive.api.routes import today as today_route


def _suggestion(target: str, created_at: str, routed_to: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(target=target, created_at=created_at, route_to_person_id=routed_to)


def _stub(monkeypatch: pytest.MonkeyPatch, items: list[SimpleNamespace] | Exception) -> None:
    def pending(*a: object, **k: object) -> list[SimpleNamespace]:
        if isinstance(items, Exception):
            raise items
        return items

    monkeypatch.setattr("openexecutive.monitoring.store.list_pending_suggestions", pending)


def test_no_suggestions_means_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [])
    assert today_route._waiting_elsewhere() == []


def test_suggestions_make_one_row_naming_the_first_two(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        [
            _suggestion("https://www.example.com/legal", "2026-01-02T10:00:00+00:00"),
            _suggestion("https://news.example.org/", "2026-01-04T10:00:00+00:00"),
            _suggestion("INITECH", "2026-01-03T10:00:00+00:00"),
            _suggestion("acme pricing", "2026-01-01T10:00:00+00:00"),
        ],
    )
    [row] = today_route._waiting_elsewhere()
    assert row.key == "watch-suggestions"
    assert row.title == "4 things I'd like to watch"
    assert row.detail == "example.com, news.example.org and 2 more"
    assert row.count == 4
    assert row.href == "/watchlist"
    assert row.action == "Look"
    assert row.at == "2026-01-04T10:00:00+00:00"


def test_one_suggestion_reads_singular(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_suggestion("INITECH", "2026-01-02T10:00:00+00:00")])
    [row] = today_route._waiting_elsewhere()
    assert row.title == "1 thing I'd like to watch"
    assert row.detail == "INITECH"


def test_two_suggestions_are_both_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_suggestion("A", "x"), _suggestion("B", "y")])
    [row] = today_route._waiting_elsewhere()
    assert row.detail == "A and B"


def test_a_store_failure_leaves_home_working(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, RuntimeError("db gone"))
    assert today_route._waiting_elsewhere() == []


def test_a_malformed_url_target_reads_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_suggestion("https://[news] acme pricing", "x")])
    [row] = today_route._waiting_elsewhere()
    assert row.detail == "https://[news] acme pricing"


def test_repeated_hosts_are_named_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_suggestion(f"https://acme.com/{p}", "x") for p in "abc"])
    [row] = today_route._waiting_elsewhere()
    assert row.title == "3 things I'd like to watch"
    assert row.detail == "acme.com"
    _stub(monkeypatch, [_suggestion(t, "x") for t in ("https://a.com/1", "https://a.com/2", "B", "C")])
    [row] = today_route._waiting_elsewhere()
    assert row.detail == "a.com, B and 1 more"


def test_suggestions_routed_to_a_head_are_theirs(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, [_suggestion("A", "x", routed_to=7), _suggestion("B", "y")])
    [row] = today_route._waiting_elsewhere()
    assert (row.count, row.detail) == (1, "B")
    _stub(monkeypatch, [_suggestion("A", "x", routed_to=7)])
    assert today_route._waiting_elsewhere() == []
