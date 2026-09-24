"use client";

import { Suspense } from "react";
import KnowledgeWorkspace from "@/components/knowledge/KnowledgeWorkspace";

export default function KnowledgePage() {
  return (
    <main className="flex-1 min-h-0 overflow-hidden">
      {/* useSearchParams (for ?view=review) needs a Suspense boundary. */}
      <Suspense>
        <KnowledgeWorkspace />
      </Suspense>
    </main>
  );
}
