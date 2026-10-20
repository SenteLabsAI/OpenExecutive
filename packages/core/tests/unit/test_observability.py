"""Logfire tracing: off without a token, and never exports user content when on."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import logfire
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from logfire.testing import SimpleSpanProcessor, TestExporter

import openexecutive.observability as observability

SECRET = "ACME-ACQUISITION-SECRET"

_USAGE = SimpleNamespace(
    input_tokens=1200,
    output_tokens=80,
    cache_read_input_tokens=1000,
    cache_creation_input_tokens=0,
)
_MESSAGE = SimpleNamespace(usage=_USAGE, stop_reason="end_turn", content=[SECRET])


class _FailingProvider:
    """Mimics an SDK 400 whose error body echoes the request."""

    async def messages_create(self, **kwargs: Any) -> Any:
        raise ValueError(f"Error code: 400 - messages.0.content: {SECRET}")

    def messages_stream(self, **kwargs: Any) -> Any:
        @asynccontextmanager
        async def cm() -> Any:
            raise ValueError(f"Error code: 400 - messages.0.content: {SECRET}")
            yield

        return cm()


class _FakeProvider:
    async def messages_create(self, **kwargs: Any) -> Any:
        return _MESSAGE

    def messages_stream(self, **kwargs: Any) -> Any:
        @asynccontextmanager
        async def cm() -> Any:
            yield SimpleNamespace(current_message_snapshot=_MESSAGE)

        return cm()


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> TestExporter:
    """Route Logfire to an in-memory exporter and turn tracing on."""
    exporter = TestExporter()
    real_configure = logfire.configure

    def local_configure(**kwargs: Any) -> Any:
        kwargs.pop("token", None)
        return real_configure(
            **kwargs,
            send_to_logfire=False,
            additional_span_processors=[SimpleSpanProcessor(exporter)],
        )

    monkeypatch.setattr(logfire, "configure", local_configure)
    monkeypatch.setattr(observability, "_logfire_token", lambda: "test-token")
    return exporter


def _exported_text(exporter: TestExporter) -> str:
    # exported_spans_as_dict() omits span status, which OTLP does export.
    statuses = [span.status.description for span in exporter.exported_spans]
    return json.dumps([exporter.exported_spans_as_dict(), statuses], default=str)


def test_disabled_without_token() -> None:
    app = FastAPI()
    observability.configure_tracing(app)

    provider = _FakeProvider()
    assert observability.tracing_enabled() is False
    assert observability.wrap_provider(provider, "anthropic") is provider  # type: ignore[arg-type]
    with observability.trace_span("noop") as span:
        assert span is None


async def test_provider_spans_record_usage_but_not_content(exporter: TestExporter) -> None:
    observability.configure_tracing(FastAPI())
    provider = observability.wrap_provider(_FakeProvider(), "anthropic")  # type: ignore[arg-type]
    request = {
        "model": "claude-sonnet-5",
        "max_tokens": 512,
        "system": SECRET,
        "messages": [{"role": "user", "content": SECRET}],
    }

    assert await provider.messages_create(**request) is _MESSAGE
    async with provider.messages_stream(**request) as stream:
        assert stream.current_message_snapshot is _MESSAGE

    spans = {s["name"]: s for s in exporter.exported_spans_as_dict()}
    for name in ("messages.create claude-sonnet-5", "messages.stream claude-sonnet-5"):
        attrs = spans[name]["attributes"]
        assert attrs["gen_ai.request.model"] == "claude-sonnet-5"
        assert attrs["gen_ai.usage.input_tokens"] == 1200
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 1000
        assert list(attrs["gen_ai.response.finish_reasons"]) == ["end_turn"]
    assert SECRET not in _exported_text(exporter)


def test_http_spans_drop_bodies_and_query_strings(exporter: TestExporter) -> None:
    app = FastAPI()

    @app.post("/chat")
    async def chat(body: dict[str, str]) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/search")
    async def search(q: str) -> dict[str, bool]:
        return {"ok": True}

    observability.configure_tracing(app)
    client = TestClient(app)
    assert client.post("/chat", json={"message": SECRET}).status_code == 200
    assert client.get("/search", params={"q": SECRET}).status_code == 200

    names = [s["name"] for s in exporter.exported_spans_as_dict()]
    assert "POST /chat" in names and "GET /search" in names
    assert SECRET not in _exported_text(exporter)


def test_traced_decorator_keeps_return_value_and_skips_arguments(exporter: TestExporter) -> None:
    observability.configure_tracing(FastAPI())

    @observability.traced("knowledge retrieve")
    def retrieve(query: str) -> str:
        return query.upper()

    assert retrieve(SECRET.lower()) == SECRET
    assert [s["name"] for s in exporter.exported_spans_as_dict()] == ["knowledge retrieve"]
    assert SECRET.lower() not in _exported_text(exporter)


async def test_failed_model_call_records_error_type_not_message(exporter: TestExporter) -> None:
    observability.configure_tracing(FastAPI())
    provider = observability.wrap_provider(_FailingProvider(), "anthropic")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await provider.messages_create(model="claude-sonnet-5", messages=[])

    (span,) = exporter.exported_spans_as_dict()
    assert span["attributes"]["exception.type"] == "ValueError"
    assert SECRET not in _exported_text(exporter)


def test_trace_span_exception_is_recorded_by_type_only(exporter: TestExporter) -> None:
    observability.configure_tracing(FastAPI())

    with pytest.raises(ValueError), observability.trace_span("specialist {specialist}", specialist="cfo"):
        raise ValueError(SECRET)

    assert SECRET not in _exported_text(exporter)


def test_http_spans_drop_path_params_errors_and_caller_identity(exporter: TestExporter) -> None:
    app = FastAPI()

    @app.get("/documents/{filename}")
    async def document(filename: str) -> dict[str, bool]:
        raise HTTPException(status_code=404, detail=f"No document {filename}")

    @app.get("/boom")
    async def boom() -> dict[str, bool]:
        raise RuntimeError(SECRET)

    observability.configure_tracing(app)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {
        "user-agent": SECRET,
        "baggage": f"leak={SECRET}",
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    }
    assert client.get(f"/documents/{SECRET}.pdf", headers=headers).status_code == 404
    assert client.get("/boom").status_code == 500

    requests = [s for s in exporter.exported_spans_as_dict() if s["name"].startswith("GET ")]
    doc_span = next(s for s in requests if s["name"] == "GET /documents/{filename}")
    assert doc_span["attributes"]["http.target"] == "/documents/{filename}"
    assert doc_span["parent"] is None  # incoming traceparent ignored
    assert SECRET not in _exported_text(exporter)
    assert "testserver" not in _exported_text(exporter)


async def test_stream_stopped_early_still_records_usage(exporter: TestExporter) -> None:
    observability.configure_tracing(FastAPI())
    provider = observability.wrap_provider(_FakeProvider(), "anthropic")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        async with provider.messages_stream(model="claude-sonnet-5", messages=[]):
            raise RuntimeError("user pressed Stop")

    (span,) = exporter.exported_spans_as_dict()
    assert span["attributes"]["exception.type"] == "RuntimeError"
    assert span["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 1000
