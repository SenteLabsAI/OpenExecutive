"""Charter text for the 8 catalogue departments.

No install starts with these: a department is created by setup or the
Executive's ``create_goal`` tool, and one named after a catalogue area starts
from that area's charter (``store.default_charter_for``). Phase 1 fleshes out
Finance only; the other 7 ship as two-line stubs. Charters are copied into the
`departments` table when the department is created — editing a charter at
runtime goes through the PATCH route, not by editing this file.
"""
from __future__ import annotations

from openexecutive.departments.models import DepartmentCharter

# Cadence spec used when seeding fresh departments. Phase 5 parses these
# strings; until then they are just text on the row. Centralised here so the
# eight seed entries below stay in sync.
DEFAULT_CHECK_IN_CADENCE: str = "daily@09:00"

FINANCE_CHARTER = DepartmentCharter(
    mission=(
        "Own the company's capital position, unit economics, financial planning, "
        "and investor reporting."
    ),
    scope=[
        "monthly close",
        "FP&A modelling",
        "fundraising preparation",
        "vendor contracts >$10K",
        "board financials",
    ],
    out_of_scope=[
        "pricing strategy (Product)",
        "payroll mechanics (HR Ops)",
        "procurement <$10K (Operations)",
    ],
)


def _stub(mission: str) -> DepartmentCharter:
    return DepartmentCharter(mission=mission, scope=[], out_of_scope=[])


STRATEGY_CHARTER = _stub(
    "Set company direction: competitive positioning, market entry, "
    "scenario planning, and long-range OKRs."
)
HR_CHARTER = _stub(
    "Run the people function: hiring, compensation, performance, "
    "culture, and org design."
)
LEGAL_CHARTER = _stub(
    "Manage legal exposure: contracts, IP, employment basics, and "
    "compliance (with appropriate disclaimers)."
)
OPERATIONS_CHARTER = _stub(
    "Run the operating layer: process design, vendor management, "
    "operational scaling, and metrics."
)
MARKETING_CHARTER = _stub(
    "Own GTM strategy, brand, messaging, PR, and crisis communications."
)
PRODUCT_CHARTER = _stub(
    "Own the product roadmap, prioritization frameworks, and product strategy."
)
BOARD_COMMS_CHARTER = _stub(
    "Prepare board decks, investor updates, and governance communications."
)


# slug -> (title, specialist_key, charter)
DEFAULT_DEPARTMENTS: tuple[tuple[str, str, str, DepartmentCharter], ...] = (
    ("strategy", "Strategy", "cso", STRATEGY_CHARTER),
    ("finance", "Finance", "cfo", FINANCE_CHARTER),
    ("hr", "People & Talent", "chro", HR_CHARTER),
    ("legal", "Legal", "gc", LEGAL_CHARTER),
    ("operations", "Operations", "coo", OPERATIONS_CHARTER),
    ("marketing", "Marketing", "cmo", MARKETING_CHARTER),
    ("product", "Product", "cpo", PRODUCT_CHARTER),
    ("board_comms", "Board & Investor Comms", "board_comms", BOARD_COMMS_CHARTER),
)
