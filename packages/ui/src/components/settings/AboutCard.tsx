"use client";

import { useEffect, useState } from "react";

import { getVersion } from "@/lib/api";
import { versionNotice, type VersionNotice } from "@/lib/versionNotice";

// Settings → About: the running version, and a link to the newer release
// and the upgrade steps when one is out (GET /version). The page supplies
// the section heading; this is the body.
export default function AboutCard() {
  const [notice, setNotice] = useState<VersionNotice | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();
    getVersion(ctrl.signal)
      .then((v) => setNotice(versionNotice(v)))
      .catch((err) => {
        if (!ctrl.signal.aborted) {
          console.warn("version check failed", err);
          setFailed(true);
        }
      });
    return () => ctrl.abort();
  }, []);

  if (failed) {
    return <p className="text-[15px] text-fg-muted">Couldn&apos;t load the version.</p>;
  }
  if (!notice) {
    return <p className="text-[15px] text-fg-muted">Loading…</p>;
  }
  return (
    <div className="space-y-1.5">
      <p className="text-lg font-semibold text-fg">{notice.running}</p>
      <p className="text-[15px] text-fg-muted">{notice.status}</p>
      {notice.update && (
        <p className="pt-1 text-[15px] font-medium">
          <a
            href={notice.update.releaseUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent hover:underline"
          >
            What&apos;s new
          </a>
          <span className="text-fg-subtle"> · </span>
          <a
            href={notice.update.upgradeUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent hover:underline"
          >
            How to upgrade
          </a>
        </p>
      )}
    </div>
  );
}
