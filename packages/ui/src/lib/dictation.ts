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

/** Whether live words may run beside the recording. Not on Android, where
 *  the phone gives the microphone to one listener at a time: while the
 *  browser's recognition runs, the recording is silent. "Request desktop
 *  site" drops Android from the user agent, so the platform is checked too. */
export function liveWordsAllowed(userAgent: string, platform = ""): boolean {
  return !/Android/i.test(userAgent) && !/Android/i.test(platform);
}

/** One piece of what the browser's own speech recognition heard so far. */
export interface HeardPart {
  transcript: string;
  isFinal: boolean;
}

/** The words shown while the person talks: `settled` (what the browser
 *  won't change any more, after `earlier`) and `pending` (its current
 *  guess at the latest words, which may still change). */
export function splitHeard(earlier: string, parts: HeardPart[]): { settled: string; pending: string } {
  let settled = earlier.trim();
  let pending = "";
  for (const part of parts) {
    const text = part.transcript.trim();
    if (!text) continue;
    if (part.isFinal) settled = joinDictation(settled, text);
    else pending = joinDictation(pending, text);
  }
  return { settled, pending };
}

/** The two short tones: rising when the mic starts, falling when it stops. */
export const MIC_TONES: Record<"start" | "stop", readonly [number, number]> = {
  start: [660, 880],
  stop: [880, 660],
};

/** How loud the mic is, 0 to 1, from the loudness of the latest sound
 *  (`rms`, 0 to 1). Speech is quiet next to full scale, so it is raised;
 *  it rises at once and falls back slowly, so the bars don't flicker. */
export function micLevel(rms: number, previous: number): number {
  const now = Math.min(1, Math.max(0, rms) * 5);
  return Math.max(now, previous * 0.85);
}

// The bars' shape at full voice: tallest in the middle.
const BAR_SHAPE = [0.55, 0.8, 1, 0.8, 0.55];

/** The five bars in the mic button while it listens, each a fraction of
 *  its full height: short and even when quiet, growing with the voice. */
export function micBars(level: number): number[] {
  const l = Math.min(1, Math.max(0, level));
  return BAR_SHAPE.map((s) => 0.2 + 0.8 * s * l);
}
