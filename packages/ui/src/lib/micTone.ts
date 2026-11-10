// The mic's sound: a short two-note tone when it starts listening and when
// it stops, so the person hears that the tap took. Quiet, about a quarter
// of a second, and silent wherever the browser won't play sound.
import { MIC_TONES } from "@/lib/dictation";

let audio: AudioContext | null = null;

export function playMicTone(kind: "start" | "stop"): void {
  try {
    const Ctx =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    audio ??= new Ctx();
    if (audio.state === "suspended") void audio.resume();
    const now = audio.currentTime;
    MIC_TONES[kind].forEach((freq, i) => {
      const at = now + i * 0.09;
      const osc = audio!.createOscillator();
      const gain = audio!.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(0.15, at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.14);
      osc.connect(gain);
      gain.connect(audio!.destination);
      osc.start(at);
      osc.stop(at + 0.16);
    });
  } catch {
    // No sound is fine: the button still shows it's listening.
  }
}
