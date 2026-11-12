// How loud the mic is while it listens, for the bars in the mic button.
// Reads the recording's own stream, so it shows sound reaching the
// recording on every browser. Calls `onLevel` (0 to 1) about every frame
// until the returned stop is called; does nothing where the browser has
// no audio processing.
import { micLevel } from "@/lib/dictation";

export function watchMicLevel(stream: MediaStream, onLevel: (level: number) => void): () => void {
  let ctx: AudioContext | undefined;
  let frame = 0;
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return () => {};
    const audio = new Ctx();
    ctx = audio;
    const analyser = audio.createAnalyser();
    analyser.fftSize = 512;
    audio.createMediaStreamSource(stream).connect(analyser);
    const samples = new Uint8Array(analyser.fftSize);
    let level = 0;
    const tick = () => {
      analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const s of samples) {
        const v = (s - 128) / 128;
        sum += v * v;
      }
      level = micLevel(Math.sqrt(sum / samples.length), level);
      onLevel(level);
      frame = requestAnimationFrame(tick);
    };
    if (audio.state === "suspended") void audio.resume();
    frame = requestAnimationFrame(tick);
  } catch {
    // A stream it can't read: let the context go.
    void ctx?.close().catch(() => {});
    return () => {};
  }
  return () => {
    cancelAnimationFrame(frame);
    void ctx?.close().catch(() => {});
  };
}
