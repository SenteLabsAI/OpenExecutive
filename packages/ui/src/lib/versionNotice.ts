// Settings → About: what to say about the running version and the latest
// release, from GET /version. Kept free of React so scripts/ can test it.

export const UPGRADE_DOC_URL =
  "https://github.com/SenteLabsAI/OpenExecutive/blob/main/docs/deployment.md#upgrading";

export interface VersionFacts {
  current: string;
  latest: string | null;
  update_available: boolean;
  release_url: string | null;
  check_enabled: boolean;
}

export interface VersionNotice {
  /** "Open Executive v0.4.4" */
  running: string;
  /** One line on how it compares with the latest release. */
  status: string;
  /** Set only when a newer release is out. */
  update: { releaseUrl: string; upgradeUrl: string } | null;
}

export function versionNotice(v: VersionFacts): VersionNotice {
  const running = `Open Executive v${v.current}`;
  if (!v.check_enabled) {
    return {
      running,
      status: "Checking for new releases is turned off (UPDATE_CHECK_ENABLED).",
      update: null,
    };
  }
  if (!v.latest) {
    return { running, status: "Couldn't reach GitHub to check for a newer release.", update: null };
  }
  if (v.update_available && v.release_url) {
    return {
      running,
      status: `v${v.latest} is available.`,
      update: { releaseUrl: v.release_url, upgradeUrl: UPGRADE_DOC_URL },
    };
  }
  if (v.latest === v.current) {
    return { running, status: "This is the latest release.", update: null };
  }
  return { running, status: `The latest release is v${v.latest}.`, update: null };
}
