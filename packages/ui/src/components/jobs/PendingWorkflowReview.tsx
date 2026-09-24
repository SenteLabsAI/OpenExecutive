"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { DynamicWorkflowDef, Person, listPeople } from "@/lib/api";
import WorkflowDraftReview from "./WorkflowDraftReview";

/**
 * A custom workflow that is switched off — typically one chat saved with
 * tool steps. It can't run until someone turns it on here, after seeing the
 * same review card (steps, tools, what may change things) the wizard shows.
 */
export default function PendingWorkflowReview({
  definition,
  onActivated,
}: {
  definition: DynamicWorkflowDef;
  onActivated: () => void;
}) {
  const [people, setPeople] = useState<Person[]>([]);

  useEffect(() => {
    // Names only (for sign-off and delivery rows); ids still show without them.
    listPeople()
      .then(setPeople)
      .catch(() => setPeople([]));
  }, []);

  return (
    <div className="h-full overflow-y-auto bg-surface text-fg">
      <div className="mx-auto max-w-3xl px-4 py-6 space-y-4">
        <Link href="/jobs" className="text-sm text-fg-muted hover:text-fg">
          ← Back to workflows
        </Link>
        <p className="text-sm text-fg-muted">
          This workflow is off. Review what it does and which tools it uses, then
          turn it on to run it or start its schedule.
        </p>
        <WorkflowDraftReview
          draft={{ definition, summary: "", assumptions: [] }}
          people={people}
          pending={{ onActivated }}
        />
      </div>
    </div>
  );
}
