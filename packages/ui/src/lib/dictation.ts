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

/** One piece of what the browser's own speech recognition heard so far. */
export interface HeardPart {
  transcript: string;
  isFinal: boolean;
}

/** The words shown while the person talks: `settled` (what the browser
 *  won't change any more, after `earlier`) and `pending` (its current
 *  guess at the latest words, which may still change). */
export function splitHeard(
  earlier: string,
  parts: HeardPart[],
  cumulative = false,
): { settled: string; pending: string } {
  // Chrome on Android (`cumulative`) sends each piece with everything said
  // before it ("hi", "hi there", "hi there how"), where desktop sends only
  // the new words. There a piece that starts with the one before it
  // replaces that one, so the words don't repeat.
  const pieces: HeardPart[] = [];
  for (const part of parts) {
    const text = part.transcript.trim();
    if (!text) continue;
    const prev = pieces[pieces.length - 1];
    // Android may add a comma or full stop as it revises ("hello" then
    // "hello, how are you"), so trailing punctuation doesn't count.
    const before = prev?.transcript.toLowerCase().replace(/[.,;:!?]+$/, "");
    const lower = text.toLowerCase();
    const next = lower.charAt(before?.length ?? 0);
    if (cumulative && before && lower.startsWith(before) && (!next || /[\s.,;:!?]/.test(next))) pieces.pop();
    pieces.push({ transcript: text, isFinal: part.isFinal });
  }
  let settled = earlier.trim();
  let pending = "";
  for (const part of pieces) {
    if (part.isFinal) settled = joinDictation(settled, part.transcript);
    else pending = joinDictation(pending, part.transcript);
  }
  return { settled, pending };
}

/** The two short tones: rising when the mic starts, falling when it stops. */
export const MIC_TONES: Record<"start" | "stop", readonly [number, number]> = {
  start: [660, 880],
  stop: [880, 660],
};
