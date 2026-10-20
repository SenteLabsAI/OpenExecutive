"use client";

import { Suspense } from "react";

import PulsePage from "@/components/memories/PulsePage";

export default function MemoriesPage() {
  return (
    <main className="flex-1 min-h-0 overflow-y-auto">
      {/* useSearchParams (for ?tab=corrections) needs a Suspense boundary. */}
      <Suspense>
        <PulsePage />
      </Suspense>
    </main>
  );
}
