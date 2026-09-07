from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class TargetCustomer(BaseModel):
    profile: str = ""
    pain_points: list[str] = Field(default_factory=list)


class CompetitiveLandscape(BaseModel):
    primary_competitors: list[str] = Field(default_factory=list)
    competitive_advantages: list[str] = Field(default_factory=list)


class OrgStructure(BaseModel):
    departments: list[str] = Field(default_factory=list)
    leadership_team: list[str] = Field(default_factory=list)


class StrategicPriorities(BaseModel):
    current_year: list[str] = Field(default_factory=list)
    north_star_metric: str = ""


class Culture(BaseModel):
    values: list[str] = Field(default_factory=list)
    operating_principles: list[str] = Field(default_factory=list)


class Financials(BaseModel):
    burn_rate_monthly: float | None = None
    runway_months: float | None = None
    key_metrics: dict[str, Any] = Field(default_factory=dict)


class CompanyProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = ""
    industry: str = ""
    stage: str = ""
    founding_year: int | None = None
    headcount: int | None = None
    annual_revenue_arr: float | None = None
    mission: str = ""
    vision: str = ""
    target_customer: TargetCustomer = Field(default_factory=TargetCustomer)
    competitive_landscape: CompetitiveLandscape = Field(
        default_factory=CompetitiveLandscape
    )
    org_structure: OrgStructure = Field(default_factory=OrgStructure)
    strategic_priorities: StrategicPriorities = Field(
        default_factory=StrategicPriorities
    )
    culture: Culture = Field(default_factory=Culture)
    financials: Financials = Field(default_factory=Financials)

    @classmethod
    def load_from_yaml(cls, path: Path | str) -> CompanyProfile:
        path = Path(path)
        if not path.exists():
            return cls()
        # Explicit UTF-8: profile.yaml is routinely hand-edited, so it can hold
        # real non-ASCII text. Without this, the platform default (cp1252 on
        # Windows) silently mojibakes it into the cached company-profile prompt
        # block rather than raising.
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        company_data = data.get("company", data)
        return cls.model_validate(company_data)

    def save_to_yaml(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"company": self.model_dump()}
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=True)

    def to_prompt_block(self) -> str:
        if not self.name:
            return ""

        lines = ["## Company Context", ""]
        lines.append(f"**Company**: {self.name}")
        if self.industry:
            lines.append(f"**Industry**: {self.industry}")
        if self.stage:
            lines.append(f"**Stage**: {self.stage}")
        if self.founding_year:
            lines.append(f"**Founded**: {self.founding_year}")
        if self.headcount:
            lines.append(f"**Headcount**: {self.headcount}")
        if self.annual_revenue_arr:
            lines.append(f"**ARR**: ${self.annual_revenue_arr:,.0f}")

        if self.mission:
            lines.extend(["", f"**Mission**: {self.mission}"])
        if self.vision:
            lines.append(f"**Vision**: {self.vision}")

        if self.target_customer.profile:
            lines.extend(["", "**Target Customer**:"])
            lines.append(f"  {self.target_customer.profile}")
            if self.target_customer.pain_points:
                lines.append("  Pain points: " + "; ".join(self.target_customer.pain_points))

        if self.competitive_landscape.primary_competitors:
            lines.extend(["", "**Competitive Landscape**:"])
            lines.append(
                "  Competitors: " + ", ".join(self.competitive_landscape.primary_competitors)
            )
            if self.competitive_landscape.competitive_advantages:
                lines.append(
                    "  Our advantages: "
                    + "; ".join(self.competitive_landscape.competitive_advantages)
                )

        if self.strategic_priorities.current_year:
            lines.extend(["", "**Strategic Priorities (Current Year)**:"])
            for p in self.strategic_priorities.current_year:
                lines.append(f"  - {p}")
            if self.strategic_priorities.north_star_metric:
                lines.append(
                    f"  North Star: {self.strategic_priorities.north_star_metric}"
                )

        if self.culture.values:
            lines.extend(["", f"**Values**: {', '.join(self.culture.values)}"])

        if self.financials.burn_rate_monthly is not None:
            lines.extend(["", "**Financial Position**:"])
            lines.append(
                f"  Monthly burn: ${self.financials.burn_rate_monthly:,.0f}"
            )
            if self.financials.runway_months is not None:
                lines.append(f"  Runway: {self.financials.runway_months:.1f} months")
            for k, v in self.financials.key_metrics.items():
                lines.append(f"  {k}: {v}")

        if self.org_structure.leadership_team:
            lines.extend(["", "**Leadership**: " + ", ".join(self.org_structure.leadership_team)])

        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not bool(self.name)
