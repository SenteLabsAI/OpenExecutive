"""Act as me's ``save_my_email_attachment`` (orchestrator/mail_read_tools.py,
orchestrator/mail_file_save.py): a file from the speaker's own mail copied to
the Executive's Drive or OneDrive and shared with them alone, through the real
gateway and its gates; and the gateway keeping those files out of everyone
else's turns (delegation/saved_files.py)."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from openexecutive.config import get_settings
from openexecutive.delegation import lockdown, saved_files
from openexecutive.delegation.gmail import MailAttachment
from openexecutive.delegation.settings import DelegationOverride, pin_turn_delegation
from openexecutive.orchestrator import mail_file_save as mfs
from openexecutive.orchestrator import mail_read_tools as mr
from openexecutive.orchestrator.action_chips import SIDE_EFFECTING_TOOLS, summarize_action
from openexecutive.orchestrator.mcp_gateway import (
    MCPGateway,
    _hide_saved_files,
    code_built_call,
    set_active_gateway,
)
from openexecutive.orchestrator.schedule_tools import set_session
from openexecutive.orchestrator.session import Session
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store

# The mailbox fakes and the autouse fixtures (a temp DB, a fresh turn pin).
from .test_delegation_tools import (  # noqa: F401
    DANA,
    OWNER,
    TEAM,
    _msg,
    _session,
    db,
    fresh_turn_state,
)
from .test_mail_read_tools import Mailbox

PDF = b"%PDF-1.7 letter of intent, north site"
ASKED = "Save the LOI Priya sent to my Drive and give me the link"


@pytest.fixture
def roster() -> SimpleNamespace:
    principal = people_store.upsert_person(full_name="Olivia Owner", is_principal=True, email=OWNER)
    teammate = people_store.upsert_person(full_name="Ben Teammate", role="Ops", email=TEAM)
    people_registry.invalidate()
    return SimpleNamespace(principal=principal, teammate=teammate)


# --------------------------------------------------------------------------- #
# Fake storage behind a real MCPGateway
# --------------------------------------------------------------------------- #


class FakeStorage:
    """workspace-mcp's Drive tools and ms-365-mcp-server's OneDrive tools,
    answering as those servers do, behind the gateway's MCP session."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, str] = {}
        # Folders deleted behind the Executive's back.
        self.gone: set[str] = set()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{len(self.files) + len(self.folders) + 1:06d}abcdefghij"

    def answer(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        if name in ("google_workspace__search_drive_files", "google_workspace__list_drive_items"):
            parent = args.get("folder_id")
            found = [
                f'- Name: "{n}" (ID: {fid}, Type: application/vnd.google-apps.folder)'
                for fid, n in self.folders.items() if parent in (None, "root")
            ] + [
                f'- Name: "{f["name"]}" (ID: {fid}, Type: application/pdf) Link: https://drive.google.com/file/d/{fid}/view'
                for fid, f in self.files.items() if parent in (None, f["folder"])
            ]
            return "\n".join(["Found files:", *found]) if found else "No files found."
        if name == "google_workspace__create_drive_folder":
            fid = self._new_id("FOLDERG")
            self.folders[fid] = args["folder_name"]
            return (
                f"Successfully created folder '{args['folder_name']}' (ID: {fid}) in folder 'root' "
                f"for {args['user_google_email']}. Link: https://drive.google.com/drive/folders/{fid}"
            )
        if name == "google_workspace__create_drive_file":
            if args["folder_id"] in self.gone:
                return "Error: File not found: " + args["folder_id"]
            fid = self._new_id("FILEG")
            self.files[fid] = {
                "name": args["file_name"], "folder": args["folder_id"],
                "data": base64.b64decode(args["base64_content"]), "mime": args["content_mime_type"],
            }
            return (
                f"Successfully created file '{args['file_name']}' (ID: {fid}) in folder "
                f"'{args['folder_id']}' for {args['user_google_email']}. "
                f"Link: https://drive.google.com/file/d/{fid}/view?usp=drivesdk"
            )
        if name == "google_workspace__manage_drive_access":
            return f"Successfully shared '{args['file_id']}'"
        if name == "google_workspace__get_drive_file_content":
            return f"File: {self.files[args['file_id']]['name']}"
        if name == "microsoft_365__list-drives":
            return json.dumps({"value": [{"id": "b!Own-1", "driveType": "business"}]})
        if name == "microsoft_365__list-folder-files":
            parent = args["driveItem-id"]
            if parent == "root":
                items = [{"id": fid, "name": n, "folder": {}} for fid, n in self.folders.items()]
            else:
                items = [{"id": fid, "name": f["name"], "file": {}} for fid, f in self.files.items() if f["folder"] == parent]
            return json.dumps({"value": items})
        if name == "microsoft_365__create-onedrive-folder":
            fid = self._new_id("FOLDERM")
            self.folders[fid] = args["body"]["name"]
            return json.dumps({"id": fid, "name": args["body"]["name"], "folder": {}})
        if name == "microsoft_365__upload-file-content":
            folder, _, rest = args["driveItem-id"].partition(":/")
            if folder in self.gone:
                return json.dumps({"error": "itemNotFound"})
            fid = self._new_id("01FILEM")
            self.files[fid] = {"name": rest.rstrip(":"), "folder": folder, "data": base64.b64decode(args["body"])}
            return json.dumps({"id": fid, "name": rest.rstrip(":"), "webUrl": f"https://example.sharepoint.com/f/{fid}"})
        if name == "microsoft_365__share-drive-item":
            return json.dumps({"value": [{"id": "perm1", "roles": ["read"]}]})
        if name == "microsoft_365__search-onedrive-files":
            return json.dumps({"value": [
                *({"id": fid, "name": n, "folder": {}} for fid, n in self.folders.items()),
                *({"id": fid, "name": f["name"], "parentReference": {"id": f["folder"]}} for fid, f in self.files.items()),
            ]})
        return json.dumps({"error": f"unexpected tool {name}"})

    def named(self, name: str) -> list[dict[str, Any]]:
        return [args for called, args in self.calls if called == name]


def _gateway(storage: FakeStorage, servers: tuple[str, ...] = ("google_workspace",)) -> MCPGateway:
    gateway = MCPGateway()
    gateway.server_names = servers

    async def call_tool(_name: str, wire: dict[str, Any]) -> Any:
        result = MagicMock()
        result.content = [MagicMock(text=storage.answer(wire["tool_name"], wire["arguments"]))]
        return result

    session = MagicMock()
    session.call_tool = call_tool
    gateway._session = session
    set_active_gateway(gateway)
    return gateway


def _owner_session(*files: tuple[MailAttachment, bytes], asked: str = ASKED) -> Session:
    mailbox = Mailbox()
    mailbox.attached["m1"] = list(files) or [(_pdf("loi-north.pdf"), PDF)]
    return _session(mailbox, asked)


def _pdf(name: str, index: int = 1, size: int = len(PDF)) -> MailAttachment:
    return MailAttachment(index=index, name=name, mime_type="application/pdf", size=size)


def _save(session: Session | None, tool_input: dict[str, Any] | None = None) -> dict[str, Any]:
    async def go() -> str:
        with set_session(session):
            return await mr.handle_save_my_email_attachment(tool_input or {"message_id": "m1", "index": 1})

    return json.loads(asyncio.run(go()))


def _model_call(session: Session | None, gateway: MCPGateway, name: str, arguments: dict[str, Any]) -> str:
    async def go() -> str:
        with set_session(session):
            return await gateway.call_tool({"name": name, "arguments": arguments})

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #


def test_google_copies_the_bytes_into_the_fixed_folder_and_shares_with_the_speaker_alone(
    roster: SimpleNamespace,
) -> None:
    storage = FakeStorage()
    _gateway(storage)
    result = _save(_owner_session())
    assert result["status"] == "saved", result
    assert result["name"] == "loi-north.pdf"
    assert result["where"] == "the From your email folder in Google Drive"
    (file_id, saved), = storage.files.items()
    assert result["link"] == f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk"
    assert saved["data"] == PDF and saved["mime"] == "application/pdf"
    (folder_id, folder_name), = storage.folders.items()
    assert folder_name == mfs.FOLDER_NAME and saved["folder"] == folder_id
    create = storage.named("google_workspace__create_drive_file")[0]
    assert "fileUrl" not in create and create["user_google_email"] == get_settings().exec_email_address.lower()
    shares = storage.named("google_workspace__manage_drive_access")
    assert shares == [{
        "user_google_email": get_settings().exec_email_address.lower(),
        "file_id": file_id, "action": "grant", "share_with": OWNER,
        "role": "reader", "share_type": "user", "send_notification": False,
    }]
    assert saved_files.owner_of(mfs.GOOGLE, file_id) == roster.principal


def test_the_folder_is_made_once_and_remembered(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session((_pdf("a.pdf"), PDF), (_pdf("b.pdf", 2), PDF))
    assert _save(session, {"message_id": "m1", "index": 1})["status"] == "saved"
    assert _save(session, {"message_id": "m1", "index": 2})["status"] == "saved"
    assert len(storage.folders) == 1 and len(storage.files) == 2
    assert len(storage.named("google_workspace__create_drive_folder")) == 1
    # Made, never looked up by name.
    assert storage.named("google_workspace__search_drive_files") == []


@pytest.mark.parametrize("servers", [("google_workspace",), ("microsoft_365",)])
def test_a_folder_someone_else_named_alike_is_never_adopted(
    roster: SimpleNamespace, servers: tuple[str, ...]
) -> None:
    storage = FakeStorage()
    # A teammate made a folder of that name and shared it with themselves.
    storage.folders["FOLDERXplanted12345"] = mfs.FOLDER_NAME
    _gateway(storage, servers)
    assert _save(_owner_session())["status"] == "saved"
    (saved,) = storage.files.values()
    assert saved["folder"] != "FOLDERXplanted12345"
    assert saved["folder"] in storage.folders


@pytest.mark.parametrize("servers", [("google_workspace",), ("microsoft_365",)])
def test_a_folder_gone_is_replaced_and_the_old_one_stays_protected(
    roster: SimpleNamespace, servers: tuple[str, ...]
) -> None:
    storage = FakeStorage()
    _gateway(storage, servers)
    session = _owner_session((_pdf("a.pdf"), PDF), (_pdf("b.pdf", 2), PDF))
    assert _save(session, {"message_id": "m1", "index": 1})["status"] == "saved"
    (old,) = storage.folders
    storage.gone.add(old)
    assert _save(session, {"message_id": "m1", "index": 2})["status"] == "saved"
    assert len(storage.folders) == 2
    new = next(f for f in storage.folders if f != old)
    assert [f["folder"] for f in storage.files.values()] == [old, new]
    assert saved_files.protected(None)[1] == {old, new}


def test_saves_in_one_round_run_one_at_a_time(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session(*((_pdf(f"loi-{i}.pdf", i), PDF) for i in range(1, 5)))

    async def go() -> list[str]:
        with set_session(session):
            return await asyncio.gather(*(
                mr.handle_save_my_email_attachment({"message_id": "m1", "index": i}) for i in range(1, 5)
            ))

    results = [json.loads(r) for r in asyncio.run(go())]
    assert [r["status"] for r in results] == ["saved"] * 4
    assert len(storage.folders) == 1 and len(storage.files) == 4


# --------------------------------------------------------------------------- #
# OneDrive
# --------------------------------------------------------------------------- #


def test_onedrive_copies_into_the_fixed_folder_and_invites_the_speaker_alone(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage, ("microsoft_365",))
    result = _save(_owner_session())
    assert result["status"] == "saved", result
    assert result["where"] == "the From your email folder in OneDrive"
    (file_id, saved), = storage.files.items()
    (folder_id, _), = storage.folders.items()
    assert saved["data"] == PDF and saved["folder"] == folder_id
    assert result["link"] == f"https://example.sharepoint.com/f/{file_id}"
    upload = storage.named("microsoft_365__upload-file-content")[0]
    assert upload["drive-id"] == "b!Own-1" and upload["driveItem-id"] == f"{folder_id}:/loi-north.pdf:"
    assert storage.named("microsoft_365__share-drive-item") == [{
        "drive-id": "b!Own-1", "driveItem-id": file_id,
        "body": {"recipients": [{"email": OWNER}], "roles": ["read"], "requireSignIn": True, "sendInvitation": False},
    }]
    assert saved_files.owner_of(mfs.MICROSOFT, file_id) == roster.principal


def test_onedrive_never_overwrites_a_file_of_the_same_name(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage, ("microsoft_365",))
    session = _owner_session((_pdf("loi.pdf"), PDF), (_pdf("loi.pdf", 2), b"second"))
    assert _save(session, {"message_id": "m1", "index": 1})["name"] == "loi.pdf"
    assert _save(session, {"message_id": "m1", "index": 2})["name"] == "loi (2).pdf"
    assert sorted(f["name"] for f in storage.files.values()) == ["loi (2).pdf", "loi.pdf"]


def test_onedrive_never_overwrites_another_persons_hidden_file(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage, ("microsoft_365",))
    person = people_store.get_person(roster.teammate)
    mailbox = Mailbox(opened=TEAM)
    mailbox.attached["m1"] = [(_pdf("loi.pdf"), b"ben's")]
    ben = Session(delegation_override=DelegationOverride(enabled=True, gmail=mailbox, person=person))
    assert pin_turn_delegation(ben, "save that loi to onedrive").offered
    assert _save(ben)["name"] == "loi.pdf"
    # Olivia's listing of the folder leaves Ben's file out, but its name is taken.
    assert _save(_owner_session((_pdf("loi.pdf"), PDF)))["name"] == "loi (2).pdf"
    assert sorted(f["data"] for f in storage.files.values()) == sorted([b"ben's", PDF])


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_no_storage_says_so_and_offers_forwarding(roster: SimpleNamespace) -> None:
    result = _save(_owner_session())
    assert result["status"] == "no_storage"
    assert "forward" in result["detail"]
    storage = FakeStorage()
    _gateway(storage, ("slack",))
    assert _save(_owner_session())["status"] == "no_storage"
    assert storage.calls == []


def test_an_eval_mailbox_never_reaches_a_real_store(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session()
    session.delegation_override.gmail.saves_to_storage = False
    assert _save(session)["status"] == "no_storage"
    assert storage.calls == []


def test_a_file_too_large_is_refused_before_and_after_download(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    big = _pdf("huge.pdf", size=mr.MAX_ATTACHMENT_BYTES + 1)
    assert _save(_owner_session((big, b"")))["status"] == "too_large"
    lying = _pdf("small.pdf", size=10)
    assert _save(_owner_session((lying, b"x" * (mr.MAX_ATTACHMENT_BYTES + 1))))["status"] == "too_large"
    assert storage.calls == []


def test_a_bad_index_or_message_id_is_refused(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    assert "index is 1 to 1" in _save(_owner_session(), {"message_id": "m1", "index": 3})["error"]
    assert "message_id" in _save(_owner_session(), {"message_id": "m1", "index": 0})["error"]
    assert "isn't a message id" in _save(_owner_session(), {"message_id": "../x", "index": 1})["error"]
    assert storage.calls == []


@pytest.mark.parametrize("said", [
    "What did Priya send me?",
    "Summarise the files Priya sent",
    # Everyday words that aren't a request to save a file.
    "Keep me posted and send me a link to the thread",
    "Copy Ben on the reply and put it in the board folder",
    # Their message carries an attached file: its words aren't theirs alone.
    "Save it [Attached: notes.txt] save to drive",
])
def test_nothing_is_copied_unless_they_asked_in_their_own_words(roster: SimpleNamespace, said: str) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session(asked=said)
    result = _save(session)
    assert result["status"] == "not_asked"
    assert storage.calls == []
    # Refused before a slot was spent or their mailbox was opened.
    assert session.turn_delegation.files_saved == 0


def test_saves_are_capped_per_turn(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session()
    session.turn_delegation.files_saved = mr.SAVES_PER_TURN
    assert "file copies" in _save(session)["error"]


def test_a_failed_share_says_the_link_may_not_open_yet(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    answer = storage.answer

    def share_fails(name: str, args: dict[str, Any]) -> str:
        if name == "google_workspace__manage_drive_access":
            storage.calls.append((name, args))
            return json.dumps({"error": "sharing is off for this account"})
        return answer(name, args)

    storage.answer = share_fails  # type: ignore[method-assign]
    result = _save(_owner_session())
    assert result["status"] == "saved_not_shared"
    assert "failed" in result["detail"]
    # Recorded before the share, so it is private either way.
    (file_id, _), = storage.files.items()
    assert saved_files.owner_of(mfs.GOOGLE, file_id) == roster.principal


def test_the_gateways_share_gate_still_runs_on_the_code_built_share(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    _gateway(storage)
    session = _owner_session()
    # Their address as the mailbox says it, but not on the roster: the
    # gateway's own roster check refuses the grant.
    session.delegation_override.person.email = "olivia@elsewhere.example"
    session.delegation_override.gmail.opened = "olivia@elsewhere.example"
    result = _save(session)
    assert result["status"] == "saved_not_shared"
    assert storage.named("google_workspace__manage_drive_access") == []


# --------------------------------------------------------------------------- #
# On a turn that read their mail
# --------------------------------------------------------------------------- #


def test_it_works_on_the_turn_that_read_their_mail_and_the_bypass_is_narrow(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)
    session = _owner_session()
    session.delegation_override.gmail.threads["t1"].messages[0] = _msg(1, DANA, "LOIs attached.")

    async def read_then_save() -> tuple[str, str]:
        with set_session(session):
            read = await mr.handle_read_my_email({"thread_id": "t1"})
            saved = await mr.handle_save_my_email_attachment({"message_id": "m1", "index": 1})
            return read, saved

    read, saved = asyncio.run(read_then_save())
    assert json.loads(read)["status"] == "ok"
    assert session.turn_delegation.read_mail is True
    assert json.loads(saved)["status"] == "saved"
    assert lockdown.mail_touched_withholds("save_my_email_attachment", {}) is False
    # The model's own Drive write on that turn is still refused...
    upload = {"user_google_email": get_settings().exec_email_address, "file_name": "x.pdf",
              "base64_content": "eA==", "content_mime_type": "application/pdf", "folder_id": "root"}
    before = len(storage.calls)
    refused = _model_call(session, gateway, "google_workspace__create_drive_file", upload)
    assert "was not run" in json.loads(refused)["error"]
    # ...and so is a code-built grant for any object but the one granted.
    granted = {"name": "google_workspace__create_drive_file", "arguments": upload}

    async def lookalike() -> str:
        with set_session(session), code_built_call(granted):
            return await gateway.call_tool({"name": granted["name"], "arguments": dict(upload)})

    assert "was not run" in json.loads(asyncio.run(lookalike()))["error"]
    assert len(storage.calls) == before


def test_it_rides_with_the_other_mailbox_tools() -> None:
    from openexecutive.orchestrator.delegation_tools import (
        DELEGATION_TOOL_NAMES,
        MAILBOX_TOOL_NAMES,
    )

    assert "save_my_email_attachment" in DELEGATION_TOOL_NAMES
    assert "save_my_email_attachment" in MAILBOX_TOOL_NAMES
    assert "save_my_email_attachment" in lockdown.MAIL_TOUCHED_ALLOWED_TOOLS
    assert "save_my_email_attachment" in SIDE_EFFECTING_TOOLS
    names = [t["name"] for t in mr.MAIL_READ_TOOLS]
    assert names == sorted(names)


def test_only_a_saved_copy_earns_a_chip_without_the_senders_file_name() -> None:
    chip = summarize_action(tool_name="save_my_email_attachment", tool_input={}, tool_result=json.dumps({
        "status": "saved", "name": "Click me.pdf", "where": "the From your email folder in OneDrive",
        "link": "https://example.sharepoint.com/f/1",
    }))
    assert chip is not None and chip["summary"] == "Saved a file from your email to OneDrive"
    assert "Click me" not in json.dumps(chip) and chip["link"] is None
    assert summarize_action(
        tool_name="save_my_email_attachment", tool_input={}, tool_result=json.dumps({"status": "no_storage"})
    ) is None


# --------------------------------------------------------------------------- #
# Other people's turns can't see or open a saved file
# --------------------------------------------------------------------------- #


def _teammate_session(roster: SimpleNamespace) -> Session:
    person = people_store.get_person(roster.teammate)
    session = Session(delegation_override=DelegationOverride(enabled=True, gmail=Mailbox(opened=TEAM), person=person))
    assert pin_turn_delegation(session, "anything new?").offered
    return session


def test_google_another_persons_turn_cannot_list_or_open_the_owners_file(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)
    owner = _owner_session()
    assert _save(owner)["status"] == "saved"
    (file_id, _), = storage.files.items()
    storage.files["FILEGsomeoneelse12345"] = {"name": "team-plan.pdf", "folder": "root", "data": b""}
    search = {"user_google_email": get_settings().exec_email_address, "query": "name contains 'pdf'"}
    read = {"user_google_email": get_settings().exec_email_address, "file_id": file_id}
    for other in (_teammate_session(roster), None):
        listing = _model_call(other, gateway, "google_workspace__search_drive_files", search)
        assert file_id not in listing and "loi-north" not in listing
        assert "team-plan.pdf" in listing
        refused = json.loads(_model_call(other, gateway, "google_workspace__get_drive_file_content", read))
        assert "private to the person it was saved for" in refused["error"]
        shared = json.loads(_model_call(other, gateway, "google_workspace__manage_drive_access", {
            **read, "action": "grant", "share_with": TEAM, "role": "writer", "share_type": "user",
        }))
        assert "error" in shared
    # The owner, on their own turn, still sees and opens it.
    owner_again = _owner_session()
    assert file_id in _model_call(owner_again, gateway, "google_workspace__search_drive_files", search)
    assert "loi-north.pdf" in _model_call(owner_again, gateway, "google_workspace__get_drive_file_content", read)


def test_onedrive_another_persons_turn_loses_the_entry_from_a_json_result(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage, ("microsoft_365",))
    assert _save(_owner_session())["status"] == "saved"
    (file_id, _), = storage.files.items()
    other = _teammate_session(roster)
    found = json.loads(_model_call(other, gateway, "microsoft_365__search-onedrive-files",
                                   {"drive-id": "b!Own-1", "q": "loi"}))
    assert found == {"value": []}
    refused = _model_call(other, gateway, "microsoft_365__download-bytes",
                          {"target": f"/drives/b!Own-1/items/{file_id}/content"})
    assert "private to the person" in json.loads(refused)["error"]
    # Graph decodes the raw path, so a percent-encoded id names the same file.
    for encoded in (
        "".join(f"%{ord(c):02X}" for c in file_id),
        "".join(f"%25{ord(c):02X}" for c in file_id),
    ):
        before = len(storage.calls)
        refused = _model_call(other, gateway, "microsoft_365__download-bytes",
                              {"target": f"/drives/b!Own-1/items/{encoded}/content"})
        assert "private to the person" in json.loads(refused)["error"]
        assert storage.calls[before:] == []


def test_hiding_drops_entries_then_lines_then_masks_what_is_left() -> None:
    hidden = frozenset({"SECRETID1234567890"})
    assert _hide_saved_files("nothing here", hidden) == "nothing here"
    text = '- Name: "a" (ID: SECRETID1234567890)\n- Name: "b" (ID: OTHER)'
    assert _hide_saved_files(text, hidden) == '- Name: "b" (ID: OTHER)'
    pruned = json.loads(_hide_saved_files(
        json.dumps({"value": [{"id": "SECRETID1234567890"}, {"id": "keep"}], "count": 2}), hidden
    ))
    assert pruned == {"value": [{"id": "keep"}], "count": 2}
    assert "SECRETID" not in _hide_saved_files(json.dumps({"id": "SECRETID1234567890"}), hidden)


def test_an_unreadable_record_fails_closed_for_file_tools_only(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)

    def broken(*_a: Any, **_k: Any) -> frozenset[str]:
        raise RuntimeError("db gone")

    monkeypatch.setattr(saved_files, "protected", broken)
    for tool in (
        "google_workspace__search_drive_files", "google_workspace__get_doc_content",
        "google_workspace__read_sheet_values", "google_workspace__get_spreadsheet_info",
        "google_workspace__get_presentation", "microsoft_365__download-bytes",
        "microsoft_365__get-excel-range", "microsoft_365__create-excel-chart",
    ):
        refused = _model_call(None, gateway, tool, {"query": "x"})
        assert "Couldn't check which files are private" in json.loads(refused)["error"], tool
    assert storage.calls == []
    _model_call(None, gateway, "google_workspace__list_calendars", {})
    assert [n for n, _ in storage.calls] == ["google_workspace__list_calendars"]


def test_no_database_yet_means_no_saved_files(tmp_path: Any) -> None:
    missing = tmp_path / "none.db"
    assert saved_files.hidden_file_ids(None, db_path=missing) == frozenset()
    assert not missing.exists()


def test_a_file_keeps_its_first_owner(roster: SimpleNamespace) -> None:
    saved_files.record(roster.principal, "google", "FILEX1234567890", name="a.pdf")
    saved_files.record(roster.teammate, "google", "FILEX1234567890", name="a.pdf")
    assert saved_files.owner_of("google", "FILEX1234567890") == roster.principal
    assert saved_files.hidden_file_ids(roster.principal) == frozenset()
    assert saved_files.hidden_file_ids(roster.teammate) == {"FILEX1234567890"}


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def test_file_names_are_cleaned_and_keep_their_extension() -> None:
    assert mfs.clean_name("../../etc/passwd", 1) == "_.._etc_passwd"
    assert mfs.clean_name('LOI: "north" site?.pdf', 1) == "LOI_ _north_ site_.pdf"
    assert mfs.clean_name("a\nb.pdf", 1) == "a b.pdf"
    assert mfs.clean_name("  ...  ", 3) == "attachment-3"
    long = mfs.clean_name("x" * 300 + ".pdf", 1)
    assert len(long) <= 120 and long.endswith(".pdf")
    assert mfs.clean_mime("application/pdf") == "application/pdf"
    assert mfs.clean_mime("text/html; x=<script>") == "application/octet-stream"
    assert mfs._free_name("loi.pdf", set()) == "loi.pdf"
    assert mfs._free_name("LOI.pdf", {"loi.pdf", "loi (2).pdf"}) == "LOI (3).pdf"


# --------------------------------------------------------------------------- #
# The folder: no model call may name it, on any turn
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_quarantine() -> Any:
    saved_files._UNRECORDED.clear()
    yield
    saved_files._UNRECORDED.clear()


def test_google_no_turn_can_share_list_or_find_the_folder(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)
    assert _save(_owner_session())["status"] == "saved"
    (folder_id,) = storage.folders
    account = get_settings().exec_email_address
    for who in (_teammate_session(roster), _owner_session(asked="share the folder"), None):
        before = len(storage.calls)
        share = _model_call(who, gateway, "google_workspace__manage_drive_access", {
            "user_google_email": account, "file_id": folder_id, "action": "grant",
            "share_with": TEAM, "role": "reader", "share_type": "user",
        })
        assert "folder of files saved" in json.loads(share)["error"]
        listing = _model_call(who, gateway, "google_workspace__list_drive_items",
                              {"user_google_email": account, "folder_id": folder_id})
        assert "folder of files saved" in json.loads(listing)["error"]
        assert len(storage.calls) == before
        # A search shows neither the folder nor its id.
        found = _model_call(who, gateway, "google_workspace__search_drive_files",
                            {"user_google_email": account, "query": "name contains 'email'"})
        assert folder_id not in found and "From your email" not in found


@pytest.mark.parametrize("item", [
    "root:/From your email:",
    "root:/From%20your%20email/loi-north.pdf:",
    "root:/from your  EMAIL/loi-north.pdf:",
    "{folder}:/loi-north.pdf:",
    "{folder}",
])
@pytest.mark.parametrize("tool", ["microsoft_365__share-drive-item", "microsoft_365__copy-drive-item"])
def test_onedrive_no_turn_can_reach_the_folder_or_a_file_in_it_by_path(
    roster: SimpleNamespace, item: str, tool: str
) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage, ("microsoft_365",))
    assert _save(_owner_session())["status"] == "saved"
    (folder_id,) = storage.folders
    args: dict[str, Any] = {"drive-id": "b!Own-1", "driveItem-id": item.format(folder=folder_id)}
    if tool.endswith("share-drive-item"):
        args["body"] = {"recipients": [{"email": TEAM}], "roles": ["read"], "requireSignIn": True}
    else:
        args["body"] = {"parentReference": {"driveId": "b!Own-1", "id": "root"}}
    for who in (_teammate_session(roster), _owner_session(asked="share it with Ben")):
        before = len(storage.calls)
        refused = json.loads(_model_call(who, gateway, tool, args))
        assert "folder of files saved" in refused["error"]
        assert len(storage.calls) == before


def test_onedrive_the_owner_still_sees_their_own_file_with_its_folder_masked(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage, ("microsoft_365",))
    assert _save(_owner_session())["status"] == "saved"
    (folder_id,) = storage.folders
    (file_id,) = storage.files
    found = json.loads(_model_call(_owner_session(), gateway, "microsoft_365__search-onedrive-files",
                                   {"drive-id": "b!Own-1", "q": "loi"}))
    assert found == {"value": [{"id": file_id, "name": "loi-north.pdf", "parentReference": {"id": "[hidden]"}}]}
    assert folder_id not in json.dumps(found)


def test_a_copy_that_cant_be_recorded_is_not_shared_and_hidden_from_everyone(
    roster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk full")

    original = saved_files.record
    monkeypatch.setattr(saved_files, "record", broken)
    result = _save(_owner_session())
    assert result["status"] == "failed"
    assert "not shared" in result["detail"]
    assert storage.named("google_workspace__manage_drive_access") == []
    (file_id,) = storage.files
    monkeypatch.setattr(saved_files, "record", original)
    search = {"user_google_email": get_settings().exec_email_address, "query": "name contains 'loi'"}
    for who in (_owner_session(), _teammate_session(roster), None):
        assert file_id not in _model_call(who, gateway, "google_workspace__search_drive_files", search)


def test_a_file_saved_while_a_listing_ran_is_hidden_from_that_listing(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage)
    other = _teammate_session(roster)
    landed = "LANDEDMIDCALL1234567890"
    answer = storage.answer

    def save_lands_during_the_search(name: str, args: dict[str, Any]) -> str:
        if name != "google_workspace__search_drive_files":
            return answer(name, args)
        saved_files.record(roster.principal, "google", landed, name="loi.pdf")
        return f'Found files:\n- Name: "loi.pdf" (ID: {landed})\n- Name: "plan.txt" (ID: KEEP)'

    storage.answer = save_lands_during_the_search  # type: ignore[method-assign]
    found = _model_call(other, gateway, "google_workspace__search_drive_files", {"query": "loi"})
    assert landed not in found and "KEEP" in found


def test_a_mail_subject_that_reads_like_the_folder_is_not_refused(roster: SimpleNamespace) -> None:
    storage = FakeStorage()
    gateway = _gateway(storage, ("microsoft_365",))
    assert _save(_owner_session())["status"] == "saved"
    before = len(storage.calls)
    out = _model_call(None, gateway, "microsoft_365__list-calendar-events",
                      {"filter": "subject eq 'Re: From your email: contract'"})
    assert "folder of files saved" not in out
    assert [n for n, _ in storage.calls[before:]] == ["microsoft_365__list-calendar-events"]


@pytest.mark.parametrize("servers", [("google_workspace",), ("microsoft_365",)])
def test_a_failure_that_isnt_a_missing_folder_makes_no_new_folder(
    roster: SimpleNamespace, servers: tuple[str, ...]
) -> None:
    storage = FakeStorage()
    _gateway(storage, servers)
    session = _owner_session((_pdf("a.pdf"), PDF), (_pdf("b.pdf", 2), PDF))
    assert _save(session, {"message_id": "m1", "index": 1})["status"] == "saved"
    answer = storage.answer

    def quota_full(name: str, args: dict[str, Any]) -> str:
        if name == "google_workspace__create_drive_file":
            return "Error: The user's Drive storage quota has been exceeded."
        if name == "microsoft_365__upload-file-content":
            return json.dumps({"error": "quotaLimitReached"})
        return answer(name, args)

    storage.answer = quota_full  # type: ignore[method-assign]
    assert _save(session, {"message_id": "m1", "index": 2})["status"] != "saved"
    assert len(storage.folders) == 1
