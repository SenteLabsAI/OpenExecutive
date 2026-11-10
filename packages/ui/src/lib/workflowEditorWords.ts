// The step editor's words for what the stored workflow calls its step kinds
// and specialists (app/jobs/new). The stored values stay as they are.

import type { DYNAMIC_SPECIALISTS, DynamicStep } from "./api";

export const STEP_KIND_LABELS: Record<DynamicStep["kind"], string> = {
  specialist: "Think it through",
  action: "Do something",
  approval_gate: "Wait for an OK",
  synthesis: "Hand it over",
};

// Same words as the chat uses for each area (orchestrator/answer_sources.py).
export const DYNAMIC_SPECIALIST_AREAS: Record<(typeof DYNAMIC_SPECIALISTS)[number], string> = {
  cso: "Strategy",
  cfo: "Finance",
  chro: "People and hiring",
  gc: "Legal",
  coo: "Operations",
  cmo: "Marketing",
  cpo: "Product",
  sales: "Sales",
  board_comms: "Board and investors",
};

// The server's cap (workflows/dynamic_models.py _MAX_STEPS).
export const MAX_WORKFLOW_STEPS = 12;
