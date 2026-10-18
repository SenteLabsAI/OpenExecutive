// The chat mic: what was heard goes into the message box, where the person
// reads it and sends it like anything typed. Pure helpers, so they test
// without a browser.

/** `heard` added after what the box already holds, one space between. */
export function joinDictation(existing: string, heard: string): string {
  const words = heard.trim();
  if (!words) return existing;
  if (!existing.trim()) return words;
  return /\s$/.test(existing) ? existing + words : `${existing} ${words}`;
}

// Chrome and Firefox record webm, Safari mp4. The first the browser takes.
const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

export function pickRecordingType(isSupported: (type: string) => boolean): string | undefined {
  return RECORDING_TYPES.find((t) => {
    try {
      return isSupported(t);
    } catch {
      return false;
    }
  });
}

/** A filename whose suffix the speech service reads the format from. */
export function recordingFilename(mimeType: string): string {
  const type = mimeType.split(";")[0].trim().toLowerCase();
  if (type === "audio/mp4") return "recording.m4a";
  if (type === "audio/ogg") return "recording.ogg";
  return "recording.webm";
}

/** 0:07, 1:42 */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
