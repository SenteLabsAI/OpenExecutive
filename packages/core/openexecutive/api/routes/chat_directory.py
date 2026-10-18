"""Chat directory: the Slack, Discord and Telegram people and rooms the
bots can reach, by name, for the People and department pickers
(``integrations.chat_directory``).

- ``GET /chat-directory/people`` — Slack people, Discord server members and
  Telegram private chats the bot has seen.
- ``GET /chat-directory/channels`` — Slack channels, Discord text channels
  and Telegram groups the bot has seen.
- ``GET /chat-directory/slack/lookup?email=`` — the Slack person with that
  work email, if any.

The principal's alone, as changing the People list is (anyone while no one
is principal yet, so a first setup can use them): the lists name everyone in
the company's Slack and Discord, with their email.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from openexecutive.integrations import chat_directory

router = APIRouter()


def _require_owner(request: Request) -> None:
    from openexecutive.api.routes.chat import _caller_is_principal_or_unclaimed

    if not _caller_is_principal_or_unclaimed(request):
        raise HTTPException(status_code=403, detail="Only the principal can see the chat directory")


@router.get("/chat-directory/people", response_model=chat_directory.DirectoryListing)
async def directory_people(request: Request) -> chat_directory.DirectoryListing:
    _require_owner(request)
    return await chat_directory.list_directory("people")


@router.get("/chat-directory/channels", response_model=chat_directory.DirectoryListing)
async def directory_channels(request: Request) -> chat_directory.DirectoryListing:
    _require_owner(request)
    return await chat_directory.list_directory("channels")


@router.get("/chat-directory/slack/lookup", response_model=chat_directory.SlackMatch)
async def directory_slack_lookup(
    request: Request, email: str = Query(..., min_length=3, max_length=320)
) -> chat_directory.SlackMatch:
    _require_owner(request)
    return await chat_directory.slack_user_by_email(email)
