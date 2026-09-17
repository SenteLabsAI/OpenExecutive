"""The built-in knowledge files that SHIP with Open Executive.

This list is the *only* source of truth for content provenance. It cannot be
derived from the filesystem at runtime: `POST /knowledge/builtin` writes a
user's own document into the very same `knowledge/builtin/<domain>/` tree
(`api/routes/knowledge._resolve_path`), so "there is a .md file at this path"
is equally true of shipped content and of a confidential draft someone just
uploaded. Re-deriving provenance from the tree marked those uploads as shipped
and auto-approved them into retrieval.

`ReviewStore.sync_builtin_registrations` registers exactly these paths as
trusted defaults. Anything else in the tree belongs to the user and reaches
`review_items` through `ReviewStore.register()`, which never sets
`trusted_default`.

Regenerate after adding or removing a built-in knowledge doc:

    git ls-files 'packages/core/openexecutive/knowledge/builtin/**/*.md' \
      | grep -v '/skills/' \
      | sed 's#packages/core/openexecutive/knowledge/builtin/##' | sort

`tests/unit/test_review_store.py::test_shipped_manifest_matches_git` fails if
this drifts from the tracked files.
"""

from __future__ import annotations

# Paths are POSIX-relative to `knowledge/builtin/`.
SHIPPED_BUILTIN_FILES: frozenset[str] = frozenset(
    (
        "board/audit_and_comp_committees.md",
        "board/board_communication.md",
        "board/board_composition_and_governance.md",
        "board/ceo_succession.md",
        "board/investor_relations.md",
        "board/special_situations.md",
        "failures/board/credit-suisse.md",
        "failures/board/theranos.md",
        "failures/finance/enron.md",
        "failures/finance/silicon-valley-bank.md",
        "failures/finance/wework-ipo.md",
        "failures/hr/twitter-x-acquisition.md",
        "failures/hr/uber-culture.md",
        "failures/hr/yahoo-talent-exodus.md",
        "failures/legal/ftx-collapse.md",
        "failures/legal/waymo-v-uber.md",
        "failures/marketing/new-coke.md",
        "failures/operations/boeing-737-max.md",
        "failures/operations/spirit-airlines.md",
        "failures/product/quibi.md",
        "failures/strategy/aol-time-warner.md",
        "failures/strategy/google-glass.md",
        "failures/strategy/kodak-digital.md",
        "failures/strategy/peloton.md",
        "finance/accounting_basics.md",
        "finance/capital_allocation.md",
        "finance/financial_modeling.md",
        "finance/financial_statements.md",
        "finance/fundraising.md",
        "finance/ipo_and_late_stage_readiness.md",
        "finance/saas_metrics.md",
        "finance/unit_economics.md",
        "finance/valuation_methods.md",
        "hr/compensation_design.md",
        "hr/culture_and_values.md",
        "hr/hiring_frameworks.md",
        "hr/layoffs_and_rifs.md",
        "hr/manager_effectiveness.md",
        "hr/org_design.md",
        "hr/performance_management.md",
        "hr/talent_development.md",
        "legal/ai_and_data_law.md",
        "legal/commercial_contracts.md",
        "legal/data_privacy_and_security.md",
        "legal/employment_law_for_managers.md",
        "legal/ip_strategy.md",
        "legal/ma_legal_essentials.md",
        "legal/securities_and_cap_table.md",
        "legal/startup_legal_basics.md",
        "marketing/brand_strategy.md",
        "marketing/content_marketing.md",
        "marketing/customer_marketing.md",
        "marketing/demand_generation.md",
        "marketing/go_to_market.md",
        "marketing/positioning_and_messaging.md",
        "marketing/pricing_and_packaging.md",
        "operations/ai_and_automation.md",
        "operations/business_continuity.md",
        "operations/customer_support_operations.md",
        "operations/procurement_and_vendor_management.md",
        "operations/project_management.md",
        "operations/quality_and_process.md",
        "operations/scaling_operations.md",
        "operations/security_operations.md",
        "product/experimentation.md",
        "product/launch_playbooks.md",
        "product/prioritization_frameworks.md",
        "product/product_analytics.md",
        "product/product_discovery.md",
        "product/product_led_growth.md",
        "product/product_market_fit.md",
        "product/product_strategy.md",
        "product/roadmapping.md",
        "strategy/business_model_frameworks.md",
        "strategy/competitive_analysis.md",
        "strategy/corporate_strategy.md",
        "strategy/growth_strategy.md",
        "strategy/mergers_acquisitions.md",
        "strategy/okr_frameworks.md",
        "strategy/partnerships_and_alliances.md",
        "strategy/scenario_planning.md",
        "strategy/startup_lifecycle.md",
    )
)
