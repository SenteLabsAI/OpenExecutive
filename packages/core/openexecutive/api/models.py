from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PageFormField(BaseModel):
    """One field of a form currently on the user's screen (Ask OE panel).

    ``type`` is a UI-side hint, not a validation contract: "text" |
    "textarea" | "number" | "boolean" | "select" | "json". For ``json``
    fields the live structured value rides in ``value`` and ``description``
    documents the expected shape.
    """

    name: str = Field(..., max_length=100)
    label: str = Field(..., max_length=200)
    type: str = Field("text", max_length=20)
    # Generous cap — json-typed fields embed their schema documentation here
    # (e.g. the workflow builder's steps union + people roster).
    description: str = Field("", max_length=4000)
    options: list[str] | None = Field(None, max_length=100)
    value: Any = None
    required: bool = False


class PageFormDescriptor(BaseModel):
    form_id: str = Field(..., max_length=100)
    title: str = Field(..., max_length=200)
    description: str = Field("", max_length=1000)
    fields: list[PageFormField] = Field(default_factory=list, max_length=200)


class PageContext(BaseModel):
    """What the user is looking at when they message from the Ask OE panel.

    Rendered into the USER TURN (never a cached system block — see
    CLAUDE.md "Prompt Caching") so the Executive can explain the current
    page and, when ``form`` is present, propose values for it via the
    ``propose_form_values`` tool.
    """

    route: str = Field(..., max_length=500)
    title: str = Field(..., max_length=200)
    guide_section_id: str | None = Field(None, max_length=100)
    summary: str = Field("", max_length=2000)
    form: PageFormDescriptor | None = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=32000)
    session_id: str | None = None
    # Per-message opt-in: when true, route through Committee adversarial
    # review before streaming the (revised) response to the client.
    committee_review: bool = False
    # Set by the Ask OE side panel only; absent on the main chat page.
    page_context: PageContext | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class OnboardAnswerRequest(BaseModel):
    session_id: str
    answer: str


class OnboardStatusResponse(BaseModel):
    session_id: str
    current_step: int
    total_steps: int
    current_question: str | None
    progress_percent: int
    completed: bool


class DocumentUploadResponse(BaseModel):
    filename: str
    chunks_indexed: int
    domain: str
    status: str


class CompanyDocContent(BaseModel):
    filename: str
    content: str


class HealthResponse(BaseModel):
    status: str
    builtin_knowledge_chunks: int
    company_profile_loaded: bool
    company_name: str | None = None
    builtin_skills: int = 0
    company_skills: int = 0
    version: str = "0.1.0"


class SkillMeta(BaseModel):
    name: str
    category: str
    description: str
    when_to_use: str
    source: str
    filename: str


class SkillDetail(SkillMeta):
    body: str


class SkillCreate(BaseModel):
    name: str
    category: str
    description: str
    when_to_use: str
    body: str


class SkillListResponse(BaseModel):
    skills: list[SkillMeta]


class SkillSearchHit(BaseModel):
    name: str
    category: str
    description: str
    when_to_use: str
    source: str
    score: float


class SkillSearchResponse(BaseModel):
    results: list[SkillSearchHit]


class SessionSummary(BaseModel):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class TargetCustomerData(BaseModel):
    profile: str = ""
    pain_points: list[str] = Field(default_factory=list)


class CompetitiveLandscapeData(BaseModel):
    primary_competitors: list[str] = Field(default_factory=list)
    competitive_advantages: list[str] = Field(default_factory=list)


class OrgStructureData(BaseModel):
    departments: list[str] = Field(default_factory=list)
    leadership_team: list[str] = Field(default_factory=list)


class StrategicPrioritiesData(BaseModel):
    current_year: list[str] = Field(default_factory=list)
    north_star_metric: str = ""


class CultureData(BaseModel):
    values: list[str] = Field(default_factory=list)
    operating_principles: list[str] = Field(default_factory=list)


class FinancialsData(BaseModel):
    burn_rate_monthly: float | None = None
    runway_months: float | None = None
    key_metrics: dict = Field(default_factory=dict)


class CompanyProfileResponse(BaseModel):
    name: str
    industry: str
    stage: str
    founding_year: int | None
    headcount: int | None
    annual_revenue_arr: float | None
    mission: str
    vision: str
    target_customer: TargetCustomerData
    competitive_landscape: CompetitiveLandscapeData
    org_structure: OrgStructureData
    strategic_priorities: StrategicPrioritiesData
    culture: CultureData
    financials: FinancialsData


class CompanyProfileUpdateRequest(BaseModel):
    name: str | None = None
    industry: str | None = None
    stage: str | None = None
    founding_year: int | None = None
    headcount: int | None = None
    annual_revenue_arr: float | None = None
    mission: str | None = None
    vision: str | None = None
    target_customer: TargetCustomerData | None = None
    competitive_landscape: CompetitiveLandscapeData | None = None
    org_structure: OrgStructureData | None = None
    strategic_priorities: StrategicPrioritiesData | None = None
    culture: CultureData | None = None
    financials: FinancialsData | None = None


class CompanyProfilePutRequest(CompanyProfileUpdateRequest):
    """Full profile for create-or-replace. Only `name` is mandatory; omitted
    sections fall back to CompanyProfile defaults (empty), so a PUT is a faithful
    snapshot of the request, not a merge."""

    name: str = Field(min_length=1)

