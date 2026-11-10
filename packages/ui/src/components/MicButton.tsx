"use client";

// The chat mic. Tap to talk, tap again (or reach the time limit) to stop;
// what was heard goes into the message box to read, fix and send. A short
// rising tone says it's listening and a falling one that it stopped. Where
// the browser has its own speech recognition (Chrome, Edge, Safari), the
// words show up while the person talks (`onLive`); when they stop, the
// recording is written out as before and that version replaces the live
// words. Without it (Firefox) the words come after stopping. Hidden when
// speech to text isn't set up or the browser can't record.
import { useCallback, useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import { getVoiceStatus, transcribeRecording } from "@/lib/api";
import { clock, joinDictation, pickRecordingType, recordingFilename, splitHeard } from "@/lib/dictation";
import { playMicTone } from "@/lib/micTone";

type Phase = "idle" | "listening" | "hearing";

interface Props {
  disabled?: boolean;
  /** While listening: the words so far (`settled`) and the browser's guess
   *  at the latest ones (`pending`). Called with two empty strings as soon
   *  as listening starts, and every call is followed by one `onText`. */
  onLive?: (settled: string, pending: string) => void;
  /** The words heard, once it stops. Empty when nothing was heard. */
  onText: (text: string) => void;
  onError: (message: string | null) => void;
}

// The browser's speech recognition, where it has one. Not in every
// TypeScript DOM library, so only what the mic uses.
interface Recognition {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  abort(): void;
}
type RecognitionCtor = new () => Recognition;

function browserRecognition(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export default function MicButton({ disabled, onLive, onText, onError }: Props) {
  const [maxSeconds, setMaxSeconds] = useState<number | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const recognitionRef = useRef<Recognition | null>(null);
  // True from the start of a recording until it stops: a recognizer that
  // ends on its own starts again only while this holds.
  const wantLiveRef = useRef(false);
  const restartRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // What the browser heard, kept for when the written-out version fails.
  const heardRef = useRef("");
  // For leaving mid-recording: the box keeps what was heard and unlocks.
  const onTextRef = useRef(onText);
  useEffect(() => {
    onTextRef.current = onText;
  }, [onText]);

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

  const stopRecognition = () => {
    wantLiveRef.current = false;
    if (restartRef.current) clearTimeout(restartRef.current);
    restartRef.current = null;
    const rec = recognitionRef.current;
    recognitionRef.current = null;
    if (!rec) return;
    rec.onresult = null;
    rec.onerror = null;
    rec.onend = null;
    try {
      rec.abort();
    } catch {
      // Already ended.
    }
  };

  // Leaving the page mid-recording lets the microphone go.
  useEffect(
    () => () => {
      clearTimer();
      stopRecognition();
      const rec = recorderRef.current;
      if (rec && rec.state !== "inactive") {
        rec.onstop = null;
        rec.stop();
        rec.stream.getTracks().forEach((t) => t.stop());
        onTextRef.current(heardRef.current);
      }
    },
    [],
  );

  const stop = useCallback(() => {
    clearTimer();
    stopRecognition();
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") rec.stop();
  }, []);

  // Live words from the browser's own recognition. It ends on its own after
  // a pause or a minute, so it starts again (keeping what it had, a little
  // later each time) until the recording stops. An error other than silence
  // ends live words for this recording; the words still come when it stops.
  const listen = useCallback(() => {
    const Ctor = browserRecognition();
    if (!Ctor || !onLive) return;
    let earlier = "";
    let restarts = 0;
    // Chrome on Android plays its own sound each time recognition starts,
    // so there it runs once: after a long pause the rest comes on stop.
    // It also repeats earlier words in each piece it sends.
    const android = /Android/i.test(navigator.userAgent);
    const maxRestarts = android ? 0 : 20;
    let failed = false;
    wantLiveRef.current = true;
    const begin = () => {
      if (!wantLiveRef.current) return;
      let rec: Recognition;
      try {
        rec = new Ctor();
        rec.continuous = true;
        rec.interimResults = true;
        rec.lang = navigator.language || "en-US";
      } catch {
        return;
      }
      // Words still pending when it ends on its own are kept too.
      let latest = earlier;
      rec.onresult = (e) => {
        const parts = Array.from(e.results, (r) => ({ transcript: r[0]?.transcript ?? "", isFinal: r.isFinal }));
        const { settled, pending } = splitHeard(earlier, parts, android);
        latest = joinDictation(settled, pending);
        heardRef.current = latest;
        onLive(settled, pending);
      };
      rec.onerror = (e) => {
        if (e.error !== "no-speech") failed = true;
      };
      rec.onend = () => {
        earlier = latest;
        if (recognitionRef.current !== rec) return;
        recognitionRef.current = null;
        if (failed || restarts >= maxRestarts) return;
        restarts += 1;
        restartRef.current = setTimeout(begin, Math.min(2000, 200 * restarts));
      };
      recognitionRef.current = rec;
      try {
        rec.start();
      } catch {
        recognitionRef.current = null;
      }
    };
    begin();
  }, [onLive]);

  const start = useCallback(async () => {
    if (maxSeconds === null) return;
    onError(null);
    // The tone plays on the tap itself, before any permission question.
    playMicTone("start");
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
      playMicTone("stop");
      stream.getTracks().forEach((t) => t.stop());
      recorderRef.current = null;
      stopRecognition();
      const heard = heardRef.current;
      const mime = rec.mimeType || type || "audio/webm";
      const recording = new Blob(chunks, { type: mime });
      if (recording.size === 0) {
        onText(heard);
        setPhase("idle");
        return;
      }
      setPhase("hearing");
      try {
        const text = await transcribeRecording(recording, recordingFilename(mime));
        if (text.trim() || heard) onText(text.trim() ? text : heard);
        else {
          onText("");
          onError("Didn't catch any words. Try again.");
        }
      } catch (err) {
        // The words already on screen are kept when writing it out fails.
        onText(heard);
        if (!heard) onError(err instanceof Error ? err.message : "Couldn't hear it. Try again.");
      } finally {
        setPhase("idle");
      }
    };
    recorderRef.current = rec;
    heardRef.current = "";
    rec.start();
    onLive?.("", "");
    listen();
    const began = Date.now();
    setElapsed(0);
    setPhase("listening");
    timerRef.current = setInterval(() => {
      const secs = (Date.now() - began) / 1000;
      setElapsed(secs);
      if (secs >= maxSeconds) stop();
    }, 250);
  }, [maxSeconds, onError, onLive, onText, stop, listen]);

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
