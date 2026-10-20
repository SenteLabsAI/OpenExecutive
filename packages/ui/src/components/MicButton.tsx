"use client";

// The chat mic. Tap to talk, tap again (or reach the time limit) to stop;
// what was heard goes into the message box to read, fix and send. Hidden
// when speech to text isn't set up or the browser can't record.
import { useCallback, useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import { getVoiceStatus, transcribeRecording } from "@/lib/api";
import { clock, pickRecordingType, recordingFilename } from "@/lib/dictation";

type Phase = "idle" | "listening" | "hearing";

interface Props {
  disabled?: boolean;
  onText: (text: string) => void;
  onError: (message: string | null) => void;
}

export default function MicButton({ disabled, onText, onError }: Props) {
  const [maxSeconds, setMaxSeconds] = useState<number | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let live = true;
    const canRecord =
      typeof window !== "undefined" &&
      typeof window.MediaRecorder !== "undefined" &&
      Boolean(navigator.mediaDevices?.getUserMedia);
    if (!canRecord) return;
    void getVoiceStatus().then((status) => {
      if (live && status?.available) setMaxSeconds(status.max_seconds);
    });
    return () => {
      live = false;
    };
  }, []);

  const clearTimer = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
  };

  // Leaving the page mid-recording lets the microphone go.
  useEffect(
    () => () => {
      clearTimer();
      const rec = recorderRef.current;
      if (rec && rec.state !== "inactive") {
        rec.onstop = null;
        rec.stop();
        rec.stream.getTracks().forEach((t) => t.stop());
      }
    },
    [],
  );

  const stop = useCallback(() => {
    clearTimer();
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") rec.stop();
  }, []);

  const start = useCallback(async () => {
    if (maxSeconds === null) return;
    onError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      onError("Open Executive can't use the microphone. Allow it in your browser's site settings.");
      return;
    }
    const type = pickRecordingType((t) => MediaRecorder.isTypeSupported(t));
    const rec = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
    const chunks: Blob[] = [];
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      recorderRef.current = null;
      const mime = rec.mimeType || type || "audio/webm";
      const recording = new Blob(chunks, { type: mime });
      if (recording.size === 0) {
        setPhase("idle");
        return;
      }
      setPhase("hearing");
      try {
        const text = await transcribeRecording(recording, recordingFilename(mime));
        if (text.trim()) onText(text);
        else onError("Didn't catch any words. Try again.");
      } catch (err) {
        onError(err instanceof Error ? err.message : "Couldn't hear it. Try again.");
      } finally {
        setPhase("idle");
      }
    };
    recorderRef.current = rec;
    rec.start();
    const began = Date.now();
    setElapsed(0);
    setPhase("listening");
    timerRef.current = setInterval(() => {
      const secs = (Date.now() - began) / 1000;
      setElapsed(secs);
      if (secs >= maxSeconds) stop();
    }, 250);
  }, [maxSeconds, onError, onText, stop]);

  if (maxSeconds === null) return null;

  if (phase === "listening") {
    return (
      <button
        type="button"
        onClick={stop}
        aria-label="Stop and use what you said"
        title="Stop — your words go into the message box"
        className="flex-shrink-0 h-11 px-3 rounded-xl border border-red-500/50 text-fg flex items-center gap-2 cursor-pointer"
      >
        <span className="w-2.5 h-2.5 rounded-full bg-red-500 animate-pulse" aria-hidden />
        <span className="text-sm tabular-nums">{clock(elapsed)}</span>
        <Icon name="stop" size="w-3.5 h-3.5" fill="currentColor" className="text-red-500" />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={() => void start()}
      disabled={disabled || phase === "hearing"}
      aria-label={phase === "hearing" ? "Writing out what you said" : "Talk instead of typing"}
      title={phase === "hearing" ? "Writing it out…" : `Talk — up to ${clock(maxSeconds)}`}
      className="flex-shrink-0 w-11 h-11 rounded-xl border border-line-strong text-fg-muted hover:text-fg hover:border-fg-muted disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex items-center justify-center cursor-pointer"
    >
      {phase === "hearing" ? (
        <span className="w-4 h-4 border-2 border-fg-muted border-t-transparent rounded-full animate-spin" aria-hidden />
      ) : (
        <Icon name="microphone" size="w-5 h-5" />
      )}
    </button>
  );
}
