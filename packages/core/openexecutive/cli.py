from __future__ import annotations

import asyncio

import click
from rich.console import Console
from rich.prompt import Prompt

console = Console()


def _local_principal_person_id() -> int | None:
    """Resolve the local operator's canonical principal for CLI admin tools."""
    from openexecutive.people.store import find_principal_person

    principal = find_principal_person()
    return principal.id if principal is not None else None


@click.group()
def cli() -> None:
    """Open Executive — your AI-powered virtual executive team."""
    pass


@cli.command()
@click.argument("question")
def ask(question: str) -> None:
    """Ask the Executive a single question."""
    asyncio.run(_ask(question))


async def _ask(question: str) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    if profile.is_empty():
        console.print(
            "[yellow]No company profile found. Run 'openexecutive onboard' to set one up.[/yellow]"
        )

    session = Session(company_profile=profile if not profile.is_empty() else None)
    retrieved = retrieve(query=question)
    episodic = format_for_prompt()

    executive = Executive()
    principal_person_id = _local_principal_person_id()

    console.print("\n[bold blue]Executive[/bold blue]\n")
    response = ""
    async for chunk in executive.stream_chat(
        user_message=question,
        session=session,
        retrieved_context=retrieved,
        episodic_context=episodic,
        person_id=principal_person_id,
        can_manage_roster=principal_person_id is not None,
    ):
        if isinstance(chunk, str):
            response += chunk
            console.print(chunk, end="", highlight=False)

    console.print("\n")


@cli.command()
def chat() -> None:
    """Start an interactive chat session with the Executive."""
    asyncio.run(_chat())


async def _chat() -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    if profile.is_empty():
        console.print(
            "[yellow]No company profile found. Run 'openexecutive onboard' first.[/yellow]\n"
        )
    else:
        console.print(f"[green]Company profile loaded: {profile.name}[/green]\n")

    session = Session(company_profile=profile if not profile.is_empty() else None)
    executive = Executive()
    principal_person_id = _local_principal_person_id()

    console.print("[bold]Open Executive[/bold] — type 'exit' to quit\n")

    while True:
        try:
            user_input = Prompt.ask("[bold cyan]You[/bold cyan]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if not user_input.strip():
            continue

        retrieved = retrieve(query=user_input)
        episodic = format_for_prompt()

        console.print("\n[bold blue]Executive[/bold blue]\n")
        async for chunk in executive.stream_chat(
            user_message=user_input,
            session=session,
            retrieved_context=retrieved,
            episodic_context=episodic,
            person_id=principal_person_id,
            can_manage_roster=principal_person_id is not None,
        ):
            console.print(chunk, end="", highlight=False)

        console.print("\n")


@cli.command("ingest-oer")
@click.option(
    "--phase",
    "phases",
    type=click.IntRange(1, 2),
    multiple=True,
    help="Restrict to phase 1 (CC BY / public domain) or 2 (CC BY-SA / MIT). Repeatable. Default: both.",
)
@click.option(
    "--source",
    "source_ids",
    multiple=True,
    help="Limit to specific source ids from sources.yaml. Repeatable.",
)
@click.option("--force", is_flag=True, help="Re-download cached files.")
def ingest_oer(phases: tuple[int, ...], source_ids: tuple[str, ...], force: bool) -> None:
    """Download and index open-licensed OER textbooks into the knowledge base."""
    asyncio.run(_ingest_oer(list(phases) or None, list(source_ids) or None, force))


async def _ingest_oer(
    phases: list[int] | None, source_ids: list[str] | None, force: bool
) -> None:
    from openexecutive.knowledge.external_sources import ingest_all

    results = await ingest_all(
        phases=phases, source_ids=source_ids, force=force, progress=console
    )

    total_chunks = sum(r.chunks for r in results)
    errors = [r for r in results if r.error]
    console.print(
        f"\n[bold]Done.[/bold] Indexed [green]{total_chunks}[/green] chunks "
        f"across {len(results) - len(errors)}/{len(results)} sources."
    )
    if errors:
        console.print("\n[red]Failures:[/red]")
        for r in errors:
            console.print(f"  - {r.source_id}: {r.error}")


@cli.command("consolidate-initiatives")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Apply the proposed merges. Default is dry-run.",
)
def consolidate_initiatives(apply_changes: bool) -> None:
    """Cluster duplicate active initiatives and (optionally) merge them.

    Run without --apply to preview the proposed consolidation. Re-run with
    --apply to commit the merges to the episodic database.

    NOTE: --apply takes an IMMEDIATE write lock on the SQLite database, so
    a concurrent chat turn that runs the background extraction pass will
    block briefly while the merge commits. For large consolidations,
    pause the API first.
    """
    asyncio.run(_consolidate_initiatives(apply_changes))


async def _consolidate_initiatives(apply_changes: bool) -> None:
    from openexecutive.memory.initiatives_consolidation import (
        apply_clusters,
        propose_clusters,
    )

    initiatives, clusters = await propose_clusters()
    if not clusters:
        console.print(
            f"[green]No duplicates detected.[/green] {len(initiatives)} active initiatives."
        )
        return

    title_by_id = {i.id: i.title for i in initiatives}
    total_members = sum(len(c.member_ids) for c in clusters)
    console.print(
        f"[bold]Proposed consolidation[/bold]: {total_members} rows → "
        f"{len(clusters)} canonical (net delete: {total_members - len(clusters)})\n"
    )
    for c in clusters:
        console.print(f"[cyan]→ {c.canonical_title}[/cyan]")
        for mid in c.member_ids:
            t = title_by_id.get(mid, f"<unknown id={mid}>")
            console.print(f"    id={mid}  {t}")
        console.print()

    if not apply_changes:
        console.print("[dim]Dry run. Re-run with --apply to merge.[/dim]")
        return

    result = apply_clusters(clusters)
    console.print(
        f"[green]Merged {result['clusters_merged']} clusters; "
        f"deleted {result['rows_deleted']} rows.[/green]"
    )


@cli.command()
def onboard() -> None:
    """Set up or update your company profile."""
    asyncio.run(_onboard())


async def _onboard() -> None:
    from openexecutive.onboarding.profile_builder import build_and_save_profile
    from openexecutive.onboarding.wizard import (
        TOTAL_STEPS,
        WizardState,
        get_current_question,
        process_answer,
    )

    console.print("[bold]Open Executive Onboarding[/bold]\n")
    console.print("This wizard will set up your company profile. Type 'skip' to skip optional questions.\n")

    state = WizardState()

    while not state.completed:
        question = get_current_question(state)
        if question is None:
            break

        step_num = state.current_step + 1
        console.print(f"[dim]Step {step_num}/{TOTAL_STEPS}[/dim]")
        console.print(f"[bold]{question}[/bold]\n")

        try:
            answer = Prompt.ask("> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Onboarding cancelled.[/yellow]")
            return

        state = process_answer(state, answer)
        console.print()

    principal_email = ""
    while not principal_email:
        try:
            candidate = Prompt.ask("Principal sign-in email").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Onboarding cancelled.[/yellow]")
            return
        if "@" not in candidate:
            console.print("[yellow]Enter the email used to sign in to the web UI.[/yellow]")
            continue
        principal_email = candidate

    profile = build_and_save_profile(state, principal_email=principal_email)
    console.print(
        f"\n[green]Company profile saved for '{profile.name}'.[/green]\n"
        "You can now start chatting: [bold]openexecutive chat[/bold]"
    )


if __name__ == "__main__":
    cli()
