"use client";

import { useEffect, useState } from "react";

import { getDelegation, type DelegationSettings } from "@/lib/api";

// GET /delegation once for the Delegate page's As you cards, so a change on
// one shows on the others. "hidden" for someone who can't have Act as me
// (GET /delegation answers null).
export type DelegationLoad = {
  settings: DelegationSettings | null;
  state: "loading" | "hidden" | "ready" | "error";
  setSettings: (next: DelegationSettings) => void;
};

export function useDelegation(): DelegationLoad {
  const [settings, setSettings] = useState<DelegationSettings | null>(null);
  const [state, setState] = useState<DelegationLoad["state"]>("loading");

  useEffect(() => {
    const controller = new AbortController();
    getDelegation(controller.signal)
      .then((next) => {
        if (!next) {
          setState("hidden");
          return;
        }
        setSettings(next);
        setState("ready");
      })
      .catch((err) => {
        if ((err as Error)?.name !== "AbortError") setState("error");
      });
    return () => controller.abort();
  }, []);

  return { settings, state, setSettings };
}
