"""Workflow ABC plus the small data types streamed during execution."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from openexecutive.knowledge.store import ChromaDBStore


class WorkflowSection(StrEnum):
    """UI grouping for workflows.

    The string value is what the API serializes and what the UI keys off
    of for sectioning and ordering. The order workflows appear within the
    list view is controlled by `SECTION_ORDER` on the frontend.
    """

    BOARD = "Board"
    CAPITAL = "Capital & Investors"
    GROWTH = "Growth & GTM"
    PRODUCT = "Product"
    PEOPLE = "People"
    RISK = "Risk, Legal & Crisis"
    OPERATING = "Operating Cadence"


class WorkflowStepDef(BaseModel):
    """A static description of one step in the workflow plan.

    Returned ahead of execution so the UI can render the plan before any
    step has started.
    """

    id: str
    title: str
    description: str


class WorkflowEvent(BaseModel):
    """A single event yielded during workflow execution.

    `type` discriminates the payload:

    - `step_start`: a step has begun. `step_id`, `step_title` set.
    - `step_done`:  a step finished. `step_id`, `summary` (short preview) set.
    - `progress`:   a running step reports activity (e.g. a workflow action
                    step calling a tool). `step_id`, `summary` set.
    - `result`:     structured output ready for programmatic consumers
                    (e.g. a chat tool that needs to iterate). `data` is the
                    typed payload; the human-targeted artifact still
                    follows in a separate `artifact` event.
    - `artifact`:   the final rendered artifact. `content` is the full Markdown.
    - `done`:       the run is complete. `run_id` set.
    - `error`:      the run failed. `message` set.
    """

    type: str
    step_id: str | None = None
    step_title: str | None = None
    summary: str | None = None
    content: str | None = None
    sources: list[str] = Field(default_factory=list)
    message: str | None = None
    run_id: str | None = None
    # Structured payload for `result` events. Stays None for every
    # other event type so existing consumers don't have to adapt.
    data: dict[str, Any] | None = None


class WorkflowMeta(BaseModel):
    """Public metadata about a workflow — used by `GET /workflows`.

    `section` drives the UI grouping on `/jobs`. The Pydantic serializer
    emits the enum's string value (e.g., "Capital & Investors").
    """

    name: str
    title: str
    description: str
    section: WorkflowSection
    estimated_minutes: int
    input_schema: dict[str, Any]
    steps: list[WorkflowStepDef]
    # True for user-created (dynamic) workflows; False for the built-ins.
    # Lets the catalog UI offer edit/delete only for custom workflows.
    is_custom: bool = False
    # True for workflows the system runs on its own (scheduler, onboarding,
    # reflection). Still runnable by hand; the catalog files them under
    # "System" instead of mixing them in with the jobs a user starts.
    background: bool = False
    # Names of the playbooks (skills) this workflow's steps follow — see
    # workflows/playbooks.py. Drives "Follows playbooks" on the workflow page
    # and "Used by workflows" on the Playbooks tab.
    playbooks: list[str] = Field(default_factory=list)


class Workflow(ABC):
    """Base class for executive-job workflows.

    Subclasses must set the class attributes below and implement
    `input_model`, `steps`, and `run`. Optionally implement
    `sample_inputs` to power the "Load sample run" button.
    """

    name: str  # registry key, e.g. "board_prep"
    title: str  # human-readable name
    description: str
    section: WorkflowSection  # UI grouping
    estimated_minutes: int = 3
    # See WorkflowMeta.background.
    background: bool = False
    # See WorkflowMeta.playbooks. Declare every playbook `run` loads via
    # workflows.playbooks.load_playbook (a test holds the two in sync).
    playbooks: tuple[str, ...] = ()

    @abstractmethod
    def input_model(self) -> type[BaseModel]:
        """Pydantic model class describing this workflow's inputs."""

    @abstractmethod
    def steps(self) -> list[WorkflowStepDef]:
        """Ordered list of steps this workflow will execute."""

    # NOTE: declared `def`, not `async def`, even though every subclass
    # implements it as `async def` with `yield` statements. An async
    # generator function returns an `AsyncIterator[T]` directly — wrapping
    # the supertype declaration in `async def` makes mypy think it's
    # `Coroutine[Any, Any, AsyncIterator[T]]` and rejects the subclass
    # overrides. See https://mypy.readthedocs.io/en/stable/more_types.html#asynchronous-iterators.
    @abstractmethod
    def run(
        self,
        inputs: BaseModel,
        store: ChromaDBStore,
    ) -> AsyncIterator[WorkflowEvent]:
        """Execute the workflow.

        Implementations are async generators (`async def` with `yield`),
        which is why this abstract method is declared *without* `async` —
        an async generator's return type is `AsyncIterator[T]`, not a
        coroutine wrapping one. See mypy's note on async iterators.

        Implementations should yield, in order:
        - one `step_start` and one `step_done` event per step
        - one `artifact` event with the final Markdown content
        - one `done` event with the run_id
        On failure, yield an `error` event and stop.
        """

    def sample_inputs(self) -> dict[str, Any] | None:
        """Return a fully-populated, realistic sample for the input form.

        Used by the UI's "Load sample run" button. Default `None` means
        no sample is offered. Concrete workflows should override this
        with a dict that validates against `input_model()`.
        """
        return None

    def meta(self) -> WorkflowMeta:
        return WorkflowMeta(
            name=self.name,
            title=self.title,
            description=self.description,
            section=self.section,
            estimated_minutes=self.estimated_minutes,
            input_schema=self.input_model().model_json_schema(),
            steps=self.steps(),
            background=self.background,
            playbooks=self.followed_playbooks(),
        )

    def followed_playbooks(self) -> list[str]:
        """Names of the playbooks this workflow's steps follow."""
        return list(self.playbooks)
