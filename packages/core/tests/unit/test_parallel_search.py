"""Scripted models drive the production chat/research callers and MCP SDK.

The HTTP fixture observes JSON-RPC bytes; it does not replace SearchRun.call or
its transport/discovery/result parsing. No paid inference or live search here.
"""
from __future__ import annotations

import copy
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic.types import Message

from openexecutive.config import Settings
from openexecutive.orchestrator.parallel_search import MCP_URL, USER_AGENT, SearchRun
from openexecutive.orchestrator.web_search_tool import build_web_search_tool, search_provider

QUERY = {"objective": "Python release news", "search_queries": ["Python current release news"]}
URL = "https://www.python.org/"


def message(*blocks: dict) -> Message:
    return Message(id="msg_test", model="claude-sonnet-5", role="assistant", type="message",
                   stop_reason="tool_use" if any(b['type'] == 'tool_use' for b in blocks) else "end_turn",
                   content=list(blocks), usage={"input_tokens": 1, "output_tokens": 1})


def call(name: str, arguments: dict, identity: str = "tool_1") -> dict:
    return {"type": "tool_use", "name": name, "id": identity, "input": arguments}


@pytest.fixture
def mcp_http(monkeypatch, tmp_path):
    from mcp.client import streamable_http

    for key in ("ENABLE_WEB_SEARCH", "WEB_SEARCH_PROVIDER", "WEB_SEARCH_ALLOWED_DOMAINS", "WEB_SEARCH_BLOCKED_DOMAINS", "PARALLEL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("openexecutive.memory.episodic.DB_PATH", tmp_path / "search-test.db")
    requests = []
    state = {"error": False, "raise": False}

    async def handle(request):
        requests.append(request)
        assert str(request.url) == MCP_URL
        assert request.headers['user-agent'] == USER_AGENT
        assert 'authorization' not in request.headers
        if state['raise']:
            raise httpx.ConnectError("private transport diagnostic", request=request)
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        if 'id' not in body:
            return httpx.Response(202)
        method = body['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'}} for name in ['web_search', 'web_fetch']]}
        elif method == 'tools/call':
            if state.get('hold') is not None:
                state['entered'].set()
                await state['hold'].wait()
            result = {'isError': state['error'], 'content': [{'type': 'text', 'text': 'quota exceeded' if state['error'] else json.dumps({'results': [{'url': URL, 'title': 'Python', 'excerpts': ['Python release notes']} ]})}]}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})

    original = streamable_http.streamablehttp_client

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, transport=httpx.MockTransport(handle))

    @asynccontextmanager
    async def transport(*args, **kwargs):
        async with original(*args, **kwargs, httpx_client_factory=factory) as streams:
            yield streams

    monkeypatch.setattr(streamable_http, 'streamablehttp_client', transport)
    monkeypatch.setattr('openexecutive.audit.log_event', lambda *a, **k: None)
    monkeypatch.setattr('openexecutive.audit.usage.log_model_usage', lambda *a, **k: None)
    yield requests, state


def tool_calls(requests):
    bodies = [json.loads(r.content) for r in requests if r.method == 'POST']
    return [b['params'] for b in bodies if b.get('method') == 'tools/call']


def scripted_stream(response):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    cm.get_final_message = AsyncMock(return_value=response)

    async def events():
        for block in response.content:
            if block.type == 'text':
                yield SimpleNamespace(type='content_block_delta', delta=SimpleNamespace(type='text_delta', text=block.text))

    cm.__aiter__ = lambda self=cm: events()
    return cm


@pytest.mark.parametrize('values,expected', [
    ({}, 'parallel'),
    ({'ENABLE_WEB_SEARCH': True}, 'anthropic'),
    ({'WEB_SEARCH_ALLOWED_DOMAINS': ['python.org']}, 'anthropic'),
    ({'WEB_SEARCH_PROVIDER': 'anthropic'}, 'anthropic'),
    ({'ENABLE_WEB_SEARCH': True, 'WEB_SEARCH_PROVIDER': 'parallel'}, 'parallel'),
])
def test_resolution(values, expected, mcp_http):
    assert search_provider(Settings(_env_file=None, **values)) == expected


def test_disabled_and_domain_policy(monkeypatch, mcp_http):
    monkeypatch.setenv('ENABLE_WEB_SEARCH', 'false')
    monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'parallel')
    assert build_web_search_tool() is None
    with pytest.raises(ValueError, match='Domain filters require'):
        Settings(_env_file=None, WEB_SEARCH_PROVIDER='parallel', WEB_SEARCH_BLOCKED_DOMAINS=['private.test'])


async def test_chat_followups_and_new_chat(monkeypatch, mcp_http):
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    observed = []
    responses = iter([
        message(call('web_search', QUERY)), message({'type': 'text', 'text': f'Source: {URL}'}),
        message(call('web_fetch', {'urls': [URL]})), message({'type': 'text', 'text': 'Fetched Python release notes'}),
        message(call('web_search', QUERY)), message({'type': 'text', 'text': 'Follow-up complete'}),
        message(call('web_search', QUERY)), message({'type': 'text', 'text': 'New chat complete'}),
    ])

    def stream(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        return scripted_stream(next(responses))

    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=stream))
    executive = Executive()
    session = Session()
    assert URL in await executive.chat('Find Python news', session)
    assert 'Fetched' in await executive.chat('Fetch that page', session)
    await executive.chat('Any more news?', session)
    await executive.chat('Find news', Session())
    calls = tool_calls(requests)
    assert [c['name'] for c in calls] == ['web_search', 'web_fetch', 'web_search', 'web_search']
    assert len({c['arguments']['session_id'] for c in calls[:3]}) == 1
    assert calls[3]['arguments']['session_id'] != calls[0]['arguments']['session_id']
    for index in [1, 3, 5, 7]:
        results = observed[index]['messages'][-1]['content']
        assert URL in json.dumps(results) and 'Python release notes' in json.dumps(results)
    tools = observed[0]['tools']
    names = [t['name'] for t in tools]
    assert names == sorted(names)
    assert not any(t.get('type', '').startswith('web_search_') for t in tools)


async def test_research_caller_consumes_search(monkeypatch, mcp_http):
    from openexecutive.agents.strategy import StrategyAgent
    from openexecutive.monitoring.research.specialist_research import research_one_specialist

    requests, _ = mcp_http
    observed = []
    finding = {'title': 'Python update', 'summary': 'Release notes', 'severity_hint': 'low', 'suggested_audience': 'noone', 'confidence': 'medium', 'relevant_urls': [URL]}
    responses = iter([message(call('web_search', QUERY)), message(call('web_fetch', {'urls': [URL]})), message(call('emit_research_findings', {'findings': [finding]}))])

    async def create(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr('openexecutive.agents.base.get_provider', lambda _: SimpleNamespace(messages_create=create))
    findings = await research_one_specialist('cso', StrategyAgent(), 'Find Python releases')
    assert findings and findings[0].relevant_urls == [URL]
    assert URL in json.dumps(observed[1]['messages'][-1])
    assert [c['name'] for c in tool_calls(requests)] == ['web_search', 'web_fetch']
    assert len({c['arguments']['session_id'] for c in tool_calls(requests)}) == 1


async def test_limits_errors_retry_and_isolation(mcp_http):
    import asyncio

    requests, state = mcp_http
    shared_provider_runs = [SearchRun(4), SearchRun(4)]
    await asyncio.gather(*(run.search(QUERY) for run in shared_provider_runs))
    state['error'] = True
    assert 'quota exceeded' in await shared_provider_runs[0].search(QUERY)
    state['error'] = False
    assert URL in await shared_provider_runs[0].fetch({'urls': [URL]})
    calls = tool_calls(requests)
    assert calls[0]['arguments']['session_id'] != calls[1]['arguments']['session_id']
    assert calls[0]['arguments']['session_id'] == calls[2]['arguments']['session_id'] == calls[3]['arguments']['session_id']
    capped = SearchRun(1)
    await capped.search(QUERY)
    count = len(requests)
    assert 'limit reached' in await capped.search(QUERY)
    assert len(requests) == count
    state['raise'] = True
    result = await SearchRun(1).search(QUERY)
    assert 'request failed' in result and 'private transport' not in result


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1', 'http://[::1]/', 'http://localhost/', 'http://user:password@example.com/', 'https://example.com/a b'])
async def test_invalid_urls_do_not_reach_mcp(url, mcp_http):
    requests, _ = mcp_http
    assert 'error' in await SearchRun(1).fetch({'urls': [url]})
    assert not requests


async def test_permission_disabled_timeout_and_cancellation(monkeypatch, mcp_http):
    import asyncio

    requests, _ = mcp_http
    monkeypatch.setenv('ENABLE_WEB_SEARCH', 'false')
    assert 'not enabled' in await SearchRun(1).search(QUERY)
    monkeypatch.delenv('ENABLE_WEB_SEARCH')
    monkeypatch.setattr('openexecutive.delegation.lockdown.outside_reach_refusal', lambda _: '{"error":"private turn"}')
    assert 'private turn' in await SearchRun(1).search(QUERY)
    assert not requests
    monkeypatch.setattr('openexecutive.delegation.lockdown.outside_reach_refusal', lambda _: None)

    async def timed_out(*args):
        raise TimeoutError

    monkeypatch.setattr('openexecutive.orchestrator.parallel_search._mcp_call', timed_out)
    assert 'timed out' in await SearchRun(1).search(QUERY)

    async def cancelled(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr('openexecutive.orchestrator.parallel_search._mcp_call', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await SearchRun(1).search(QUERY)


async def test_interleaved_chat_conversations(monkeypatch, mcp_http):
    import asyncio

    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.schedule_tools import current_session
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    rounds = {}

    def stream(**kwargs):
        identity = current_session.get().session_id
        index = rounds.get(identity, 0)
        rounds[identity] = index + 1
        response = message(call('web_search', QUERY)) if index % 2 == 0 else message({'type': 'text', 'text': 'Done'})
        return scripted_stream(response)

    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=stream))
    executive = Executive()
    sessions = [Session(), Session()]
    for _ in range(2):
        await asyncio.gather(*(executive.chat('Search news', session) for session in sessions))
    identities = [c['arguments']['session_id'] for c in tool_calls(requests)]
    assert len(set(identities)) == 2
    assert sorted(identities.count(s.session_id) for s in sessions) == [2, 2]


async def test_query_source_consumes_web_results(monkeypatch, mcp_http):
    from openexecutive.monitoring.models import WatchlistItem
    from openexecutive.monitoring.sources.query import QuerySource

    requests, _ = mcp_http
    responses = iter([
        message(call('web_search', QUERY)),
        message(call('emit_query_results', {'results': [{'title': 'Python release', 'url': URL, 'summary': 'New release'}]})),
    ])
    observed = []

    async def create(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr('openexecutive.agents.base.get_provider', lambda _: SimpleNamespace(messages_create=create))
    monkeypatch.setattr('openexecutive.monitoring.sources.query.validate_target_url', lambda _: (True, 'fixture DNS'))
    item = WatchlistItem(id=1, slug='python', signal_type='query', target='Python releases', config_json={}, trigger_json={})
    signals = await QuerySource().poll(item)
    assert signals and signals[0].provenance_url == URL
    assert URL in json.dumps(observed[1]['messages'][-1])
    assert tool_calls(requests)[0]['name'] == 'web_search'


async def test_answer_source_ui_and_same_turn_hold(monkeypatch, mcp_http):
    from openexecutive.orchestrator.answer_sources import TurnSources
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    sources = TurnSources()
    observed = []
    responses = iter([
        message(call('web_search', QUERY)),
        message(call('web_fetch', {'urls': [URL]})),
        message({'type': 'text', 'text': 'Ask again to fetch the page.'}),
    ])

    def stream(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        return scripted_stream(next(responses))

    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=stream))
    async for _ in Executive().stream_chat('Search Python news', Session(), turn_sources=sources):
        pass
    assert [c['name'] for c in tool_calls(requests)] == ['web_search']
    assert 'outside' in json.dumps(observed[2]['messages'][-1]).lower()
    assert any(s['url'] == URL for s in sources.payload()['sources'])


async def test_no_inference_or_search_key_needed_for_local_search(monkeypatch, mcp_http):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.setenv('LOCAL_MODELS_ENABLED', 'true')
    monkeypatch.setenv('LOCAL_BASE_URL', 'http://127.0.0.1:11434/v1')
    assert 'input_schema' in build_web_search_tool()
    assert URL in await SearchRun(1).search(QUERY)


async def test_actual_transport_timeout_and_cancellation(monkeypatch, mcp_http):
    import asyncio

    import openexecutive.orchestrator.parallel_search as parallel

    requests, state = mcp_http
    state['hold'] = asyncio.Event()
    state['entered'] = asyncio.Event()
    run = SearchRun(3)
    task = asyncio.create_task(run.search(QUERY))
    await asyncio.wait_for(state['entered'].wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_calls(requests)[0]['arguments']['session_id'] == run.session_id
    monkeypatch.setattr(parallel, 'MCP_TIMEOUT_SECONDS', 0.05)
    assert 'timed out' in await run.search(QUERY)


@pytest.mark.parametrize('conversation', [
    'slack:dm:U1', 'discord:dm:123', 'telegram:123',
    'google_chat:spaces/test:threads/first', 'email:private-thread@example.com',
])
async def test_rehydrated_channel_conversations(monkeypatch, mcp_http, conversation):
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    responses = iter([
        message(call('web_search', QUERY)), message({'type': 'text', 'text': 'Found sources'}),
        message(call('web_fetch', {'urls': [URL]})), message({'type': 'text', 'text': 'Read source'}),
        message(call('web_search', QUERY)), message({'type': 'text', 'text': 'New conversation'}),
    ])
    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=lambda **kw: scripted_stream(next(responses))))
    executive = Executive()
    # Each adapter reloads Session on a later inbound message. Resolve by the
    # complete conversation id, including rolling suffix/new thread identity.
    await executive.chat('Find news', Session(session_id=conversation))
    await executive.chat('Read it', Session(session_id=conversation))
    await executive.chat('Find news', Session(session_id=conversation + '~2'))
    identities = [c['arguments']['session_id'] for c in tool_calls(requests)]
    assert identities[0] == identities[1] != identities[2]
    assert all(str(uuid.UUID(identity)) == identity for identity in identities)
    assert conversation not in json.dumps(tool_calls(requests))


async def test_persisted_identity_concurrent_resolution_and_deletion(mcp_http):
    import asyncio

    from openexecutive.memory import episodic, session_store
    from openexecutive.orchestrator.session import Session

    conversation = 'telegram:private-channel~2'
    runs = [SearchRun.for_chat(Session(session_id=conversation)) for _ in range(2)]
    await asyncio.gather(*(run.search(QUERY) for run in runs))
    assert runs[0].session_id == runs[1].session_id
    episodic.initialize_db()
    session_store.create_session(conversation, 'Fixture', '2026-01-01', db_path=episodic.DB_PATH)
    assert session_store.delete_session(conversation, db_path=episodic.DB_PATH)
    replacement = SearchRun.for_chat(Session(session_id=conversation))
    await replacement.search(QUERY)
    assert replacement.session_id != runs[0].session_id


async def test_actual_research_quota_retry_and_new_run(monkeypatch, mcp_http):
    from openexecutive.agents.strategy import StrategyAgent
    from openexecutive.monitoring.research.specialist_research import research_one_specialist

    requests, state = mcp_http
    observed = []
    responses = iter([
        message(call('web_search', QUERY)), message(call('web_search', QUERY)),
        message(call('emit_research_findings', {'findings': []})),
        message(call('web_search', QUERY)), message(call('emit_research_findings', {'findings': []})),
    ])
    state['error'] = True

    async def create(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        # The first result is the explicit quota; a retry in the same model
        # loop must retain its UUID. A new research run gets a new UUID.
        if len(observed) == 2:
            assert 'quota exceeded' in json.dumps(kwargs['messages'][-1])
            state['error'] = False
        return next(responses)

    provider = SimpleNamespace(messages_create=create)
    monkeypatch.setattr('openexecutive.agents.base.get_provider', lambda _: provider)
    for _ in range(2):
        assert await research_one_specialist('cso', StrategyAgent(), 'Find Python news') == []
    identities = [c['arguments']['session_id'] for c in tool_calls(requests)]
    assert identities[0] == identities[1] != identities[2]


async def test_identity_failure_stops_request_and_disabled_search_writes_nothing(monkeypatch, mcp_http):
    from openexecutive.memory import episodic
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    monkeypatch.setenv('ENABLE_WEB_SEARCH', 'false')
    run = SearchRun.for_chat(Session(session_id='telegram:123'))
    assert 'not enabled' in await run.search(QUERY)
    assert not episodic.DB_PATH.exists()
    monkeypatch.delenv('ENABLE_WEB_SEARCH')

    def broken_mapping(_):
        raise OSError('private database diagnostic')

    monkeypatch.setattr('openexecutive.memory.session_store.search_conversation_id', broken_mapping)
    responses = iter([message(call('web_search', QUERY)), message({'type': 'text', 'text': 'Search unavailable'})])
    observed = []

    def stream(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        return scripted_stream(next(responses))

    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=stream))
    await Executive().chat('Find news', Session(session_id='telegram:123'))
    assert not requests
    assert 'request failed' in json.dumps(observed[1]['messages'][-1])
    assert 'private database' not in json.dumps(observed[1]['messages'][-1])


async def test_provider_change_waits_for_next_tool_list(monkeypatch, mcp_http):
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    requests, _ = mcp_http
    responses = iter([message(call('web_search', QUERY)), message({'type': 'text', 'text': 'Settings changed'})])
    observed = []

    def stream(**kwargs):
        observed.append(copy.deepcopy(kwargs))
        # A .env reload cannot append a native web_search to the already
        # pinned client tool list in an in-flight turn. Dispatch fails closed.
        monkeypatch.setenv('WEB_SEARCH_PROVIDER', 'anthropic')
        return scripted_stream(next(responses))

    monkeypatch.setattr('openexecutive.orchestrator.executive.get_provider', lambda _: SimpleNamespace(messages_stream=stream))
    await Executive().chat('Find news', Session())
    assert not requests
    for request in observed:
        names = [t['name'] for t in request['tools']]
        assert names.count('web_search') == 1
        assert not any(t.get('type') == 'web_search_20250305' for t in request['tools'])
