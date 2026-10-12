"""Apply (or re-apply) the Colette operating profile to the local database.

Usage:
    python -m openexecutive.cli.apply_colette_profile [--force]

The API applies the profile automatically at startup, once per profile
version. Use this to re-run it on demand; ``--force`` re-applies even if the
current version was already applied, still only filling untouched fields.
"""
from __future__ import annotations

import argparse
import logging

from openexecutive.departments.colette_profile import PROFILE_VERSION, apply_colette_profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-apply even if already applied")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from openexecutive.departments.store import initialize_db as init_departments
    from openexecutive.departments.store import seed_default_departments
    from openexecutive.memory.episodic import initialize_db as init_episodic
    from openexecutive.people.store import initialize_db as init_people

    init_episodic()
    init_people()
    init_departments()
    seed_default_departments()

    report = apply_colette_profile(force=args.force)
    print(f"profile {PROFILE_VERSION}: skipped={report.skipped}")
    print(f"  people created:      {report.people_created or '-'}")
    print(f"  departments updated: {report.departments_updated or '-'}")
    print(f"  goals added:         {report.goals_added}")
    print(f"  cadences cleared:    {report.cadences_cleared or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
