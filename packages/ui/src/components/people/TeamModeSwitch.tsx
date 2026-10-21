"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { useWorkspace } from "@/components/workspace/WorkspaceContext";
import { updateWorkspace } from "@/lib/api";

// Shown after a team member is added (or a contact moved onto the team) while
// Open Executive is used just for yourself: a team now exists, so the
// workspace switches to team mode on its own, without asking, and this says
// so. Workspace settings can switch it back.
export function TeamModeSwitch({ name, onDone }: { name: string; onDone: () => void }) {
  const { refresh } = useWorkspace();
  const [state, setState] = useState<"switching" | "done" | "failed">("switching");
  // Once per mount, even under React's double-invoked effects in dev.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    updateWorkspace({ mode: "team" })
      .then(() => refresh())
      .then(() => setState("done"))
      .catch(() => setState("failed"));
  }, [refresh]);

  if (state === "switching") return null;
  return (
    <div
      role="status"
      className="fixed bottom-4 left-4 right-4 z-50 mx-auto max-w-md rounded-2xl border border-line bg-surface p-4 shadow-2xl sm:left-auto sm:right-6"
    >
      <p className="text-[15px] text-fg">
        {state === "done" ? (
          <>
            {name} is on your team, so this is now a team workspace: departments, check-ins and the
            team pages are on.
          </>
        ) : (
          <>
            {name} is on your team, but the workspace couldn&apos;t switch to team mode. You can switch
            it in Workspace settings.
          </>
        )}
      </p>
      <div className="mt-3 flex items-center gap-4">
        <button
          type="button"
          onClick={onDone}
          className="h-10 rounded-lg bg-indigo-600 px-4 text-sm font-medium text-white hover:bg-indigo-500"
        >
          OK
        </button>
        <Link href="/settings/workspace" onClick={onDone} className="text-sm text-accent hover:underline">
          {state === "done" ? "Change in Workspace settings" : "Workspace settings"}
        </Link>
      </div>
    </div>
  );
}
