from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WIZARD_STEPS = [
    {
        "step": 0,
        "title": "Welcome",
        "question": "Welcome to Open Executive. I'm going to ask you a few questions to set up your company profile. This usually takes about 5 minutes. Let's start — what is the name of your company?",
        "field": "name",
        "required": True,
    },
    {
        "step": 1,
        "title": "Industry & Stage",
        "question": "What industry are you in, and what stage is the company at? (e.g., 'B2B SaaS, Series A' or 'Fintech startup, Pre-seed')",
        "field": "industry_and_stage",
        "required": True,
    },
    {
        "step": 2,
        "title": "Team Size",
        "question": "How many people are on the team right now, and when was the company founded?",
        "field": "headcount_and_founding",
        "required": True,
    },
    {
        "step": 3,
        "title": "Business Model",
        "question": "Describe your business model in a sentence or two: who are your customers, what do you sell them, and roughly what is your annual revenue (ARR if SaaS)?",
        "field": "business_model",
        "required": True,
    },
    {
        "step": 4,
        "title": "Competitive Landscape",
        "question": "Who are your top 2-3 competitors, and what is your primary competitive advantage over them?",
        "field": "competitive_landscape",
        "required": True,
    },
    {
        "step": 5,
        "title": "Strategic Priorities",
        "question": "What are your top 3 strategic priorities for this year? And what is your north star metric — the one number that, if it goes up, means everything is working?",
        "field": "strategic_priorities",
        "required": True,
    },
    {
        "step": 6,
        "title": "Culture & Values",
        "question": "What are your company values or operating principles? (Optional, but helps me give more culturally aligned advice)",
        "field": "culture",
        "required": False,
    },
    {
        "step": 7,
        "title": "Financial Position",
        "question": "What is your current monthly burn rate and how many months of runway do you have? (Optional, and stored locally only — never shared)",
        "field": "financials",
        "required": False,
    },
    {
        "step": 8,
        "title": "Mission & Vision",
        "question": "What is your mission — why does this company exist? And what is your 5-year vision for where it will be?",
        "field": "mission_and_vision",
        "required": False,
    },
    # --- Org chart steps (optional, Phase 3 People feature) ---
    {
        "step": 9,
        "title": "Your Identity",
        "question": (
            "What is your name and role? (e.g. 'Alex Rivera, CEO') "
            "This sets you as the principal — the fallback approver for all decisions."
        ),
        "field": "principal_identity",
        "required": False,
    },
    {
        "step": 10,
        "title": "Team Members",
        "question": (
            "List your key team members, one per line: 'Name, Role, Email or @Slack'\n"
            "Example:\n"
            "  Jamie Park, CTO, jamie@example.com\n"
            "  Sam Lee, Head of Sales, @samlee\n"
            "(Skip if you'd prefer to add them later)"
        ),
        "field": "team_members",
        "required": False,
    },
    {
        "step": 11,
        "title": "Fractional Executives",
        "question": (
            "Do you work with any fractional executives or advisors? List them one per line:\n"
            "'Name, Role (fractional), Channel, Availability, Approves'\n"
            "Example:\n"
            "  Sarah Chen, CFO (fractional), Slack, Tue 9am-1pm PT, spend>$10k\n"
            "(Skip if not applicable)"
        ),
        "field": "fractional_executives",
        "required": False,
    },
]

TOTAL_STEPS = len(WIZARD_STEPS)


@dataclass
class WizardState:
    current_step: int = 0
    answers: dict[str, Any] = field(default_factory=dict)
    completed: bool = False
    skipped_steps: list[int] = field(default_factory=list)

    def is_complete(self) -> bool:
        required_steps = [s["step"] for s in WIZARD_STEPS if s["required"]]
        return all(
            step in self.answers or step in self.skipped_steps
            for step in required_steps
        )

    def get_progress(self) -> dict[str, int]:
        answered = len(self.answers) + len(self.skipped_steps)
        return {"answered": answered, "total": TOTAL_STEPS, "percent": int(answered / TOTAL_STEPS * 100)}


def get_step(step: int) -> dict[str, Any] | None:
    if 0 <= step < len(WIZARD_STEPS):
        return WIZARD_STEPS[step]
    return None


def get_current_question(state: WizardState) -> str | None:
    step = get_step(state.current_step)
    if step is None:
        return None
    return step["question"]


def process_answer(state: WizardState, answer: str) -> WizardState:
    step_config = get_step(state.current_step)
    if step_config is None:
        return state

    answer = answer.strip()
    if answer and answer.lower() not in ("skip", "s", "n/a"):
        state.answers[step_config["field"]] = answer
    else:
        if not step_config["required"]:
            state.skipped_steps.append(state.current_step)

    state.current_step += 1
    if state.current_step >= TOTAL_STEPS:
        state.completed = True

    return state


def build_profile_from_answers(answers: dict[str, Any]) -> dict[str, Any]:
    """Convert wizard answers into a structured dict for CompanyProfile."""
    profile: dict[str, Any] = {}

    if "name" in answers:
        profile["name"] = answers["name"]

    if "industry_and_stage" in answers:
        text = answers["industry_and_stage"]
        parts = [p.strip() for p in text.split(",")]
        profile["industry"] = parts[0] if parts else text
        if len(parts) > 1:
            profile["stage"] = parts[1]

    if "headcount_and_founding" in answers:
        import re

        text = answers["headcount_and_founding"]
        nums = re.findall(r"\d+", text)
        if nums:
            if len(nums) >= 2:
                profile["headcount"] = int(nums[0])
                profile["founding_year"] = int(nums[1])
            elif len(nums) == 1:
                num = int(nums[0])
                if num > 1900:
                    profile["founding_year"] = num
                else:
                    profile["headcount"] = num

    if "business_model" in answers:
        text = answers["business_model"]
        profile["target_customer"] = {"profile": text, "pain_points": []}
        import re

        # The capture must START with a digit and the M must end a word.
        # `[\d,]+` alone matched a bare comma, so any answer with a comma
        # before an m-word — "Consultoría estratégica, marketing y
        # operaciones" — captured "," and crashed the whole wizard on
        # float(""). Requiring \b also stops "300 clientes, marketing"
        # from being read as $300M.
        arr_match = re.search(r"\$?(\d[\d,]*)\s*[Mm]\b", text)
        if arr_match:
            val = arr_match.group(1).replace(",", "")
            # Defensive: this is a best-effort parse of free text, so a
            # surprising input must never take down onboarding. Skipping
            # the field costs one profile value; raising costs the run.
            try:
                profile["annual_revenue_arr"] = float(val) * 1_000_000
            except ValueError:
                logger.warning("onboarding: could not parse ARR from %r", text[:80])

    if "competitive_landscape" in answers:
        text = answers["competitive_landscape"]
        sentences = text.split(".")
        competitors: list[str] = []
        advantages: list[str] = []

        for s in sentences:
            s = s.strip()
            if any(kw in s.lower() for kw in ["competitor", "vs", "against", "compete"]):
                import re

                names = re.findall(r"[A-Z][a-zA-Z]+", s)
                competitors.extend(names[:3])
            elif any(kw in s.lower() for kw in ["advantage", "differentiat", "better", "unique"]):
                advantages.append(s)

        profile["competitive_landscape"] = {
            "primary_competitors": competitors[:3] if competitors else [],
            "competitive_advantages": advantages[:3] if advantages else [text],
        }

    if "strategic_priorities" in answers:
        text = answers["strategic_priorities"]
        lines = [ln.strip("- •*").strip() for ln in text.split("\n") if ln.strip()]
        priorities = [ln for ln in lines if ln and len(ln) > 5][:5]

        north_star = ""
        for line in lines:
            if any(kw in line.lower() for kw in ["north star", "key metric", "metric"]):
                north_star = line
                break

        profile["strategic_priorities"] = {
            "current_year": priorities,
            "north_star_metric": north_star,
        }

    if "culture" in answers:
        text = answers["culture"]
        values = [v.strip("- •*").strip() for v in text.split(",")]
        values = [v for v in values if v and len(v) > 2][:8]
        profile["culture"] = {"values": values, "operating_principles": []}

    if "financials" in answers:
        import re

        text = answers["financials"]
        burn_match = re.search(r"\$?([\d,]+)\s*[Kk]?\s*(?:monthly|/month|per month|burn)", text)
        runway_match = re.search(r"(\d+)\s*month", text)

        fin: dict[str, Any] = {}
        if burn_match:
            val = burn_match.group(1).replace(",", "")
            multiplier = 1000 if "k" in text[burn_match.start():burn_match.end()].lower() else 1
            fin["burn_rate_monthly"] = float(val) * multiplier
        if runway_match:
            fin["runway_months"] = float(runway_match.group(1))
        profile["financials"] = fin

    if "mission_and_vision" in answers:
        text = answers["mission_and_vision"]
        sentences = [s.strip() for s in text.split(".") if s.strip()]
        if sentences:
            profile["mission"] = sentences[0]
        if len(sentences) > 1:
            profile["vision"] = ". ".join(sentences[1:])

    return profile


# --------------------------------------------------------------------------- #
# People extraction from wizard answers (Phase 3)
# --------------------------------------------------------------------------- #

def _parse_scope_token(token: str) -> str | None:
    """Map a free-text scope hint to an AuthorityScope value or None."""
    t = token.lower().strip()
    mapping = {
        "spend>10k": "spend_gt_10k",
        "spend > 10k": "spend_gt_10k",
        "spend>$10k": "spend_gt_10k",
        "spend > $10k": "spend_gt_10k",
        "spend<10k": "spend_lt_10k",
        "spend<$10k": "spend_lt_10k",
        "spend<2k": "spend_lt_2k",
        "spend<$2k": "spend_lt_2k",
        "hiring": "hiring_signoff",
        "legal": "legal_sign",
        "legal sign": "legal_sign",
        "vendor": "vendor_onboarding",
        "customer credit": "customer_credit",
        "board": "board_comms",
        "wildcard": "wildcard",
        "*": "wildcard",
    }
    return mapping.get(t)


def build_people_from_answers(answers: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract People records from the three org-chart wizard answers.

    Returns a list of dicts suitable for passing to `people.store.upsert_person`
    + `set_authority_scope` + `set_availability`. The caller is responsible for
    actually persisting them.

    If the answers are absent or unparseable, returns an empty list — never raises.
    """
    records: list[dict[str, Any]] = []

    # Step 9 — principal identity
    if "principal_identity" in answers:
        text = answers["principal_identity"].strip()
        parts = [p.strip() for p in text.split(",", 1)]
        name = parts[0] if parts else text
        role = parts[1] if len(parts) > 1 else ""
        if name:
            records.append({
                "full_name": name,
                "role": role,
                "is_principal": True,
                "authority_scope": ["wildcard"],
            })

    # Step 10 — team members: "Name, Role, Email or @Slack" per line
    if "team_members" in answers:
        for line in answers["team_members"].splitlines():
            line = line.strip("- •*").strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if not parts[0]:
                continue
            name = parts[0]
            role = parts[1] if len(parts) > 1 else ""
            contact = parts[2] if len(parts) > 2 else ""
            email = contact if "@" in contact and not contact.startswith("@") else None
            slack = contact if contact.startswith("@") else None
            records.append({
                "full_name": name,
                "role": role,
                "email": email,
                "slack_user_id": slack,
                "preferred_channel": "slack" if slack else ("email" if email else "any"),
                "is_principal": False,
                "authority_scope": [],
            })

    # Step 11 — fractional executives: "Name, Role, Channel, Availability, Approves" per line
    if "fractional_executives" in answers:
        for line in answers["fractional_executives"].splitlines():
            line = line.strip("- •*").strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if not parts[0]:
                continue
            name = parts[0]
            role = parts[1] if len(parts) > 1 else ""
            channel = parts[2].lower() if len(parts) > 2 else "any"
            if channel not in ("email", "slack", "telegram"):
                channel = "any"
            # Parse scope tokens from the last field (may be comma-joined earlier)
            scope_text = parts[-1] if len(parts) > 3 else ""
            scopes = [
                s for s in (
                    _parse_scope_token(tok)
                    for tok in scope_text.replace(";", ",").split(",")
                )
                if s
            ]
            records.append({
                "full_name": name,
                "role": role,
                "preferred_channel": channel,
                "is_principal": False,
                "authority_scope": scopes,
                "response_sla_hours": 24,
            })

    return records
