import { useId } from "react";

type BrandMarkSize = "sm" | "md" | "lg";

const BOX_CLASSES: Record<BrandMarkSize, string> = {
  sm: "w-7 h-7 rounded-lg bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-500/20",
  md: "w-7 h-7 rounded-full bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-500/20",
  lg: "w-12 h-12 rounded-2xl bg-indigo-600 flex items-center justify-center shadow-xl shadow-indigo-500/25",
};

const MARK_CLASSES: Record<BrandMarkSize, string> = {
  sm: "w-7 h-7 text-white",
  md: "w-7 h-7 text-white",
  lg: "w-12 h-12 text-white",
};

// The nine specialists' seats, on a ring of radius 14 around the Executive at
// (24, 24), starting at twelve o'clock.
const SEATS: ReadonlyArray<readonly [number, number]> = [
  [24, 10],
  [33, 13.28],
  [37.79, 21.57],
  [36.12, 31],
  [28.79, 37.16],
  [19.21, 37.16],
  [11.88, 31],
  [10.21, 21.57],
  [15, 13.28],
];

/**
 * The Council mark alone: the Executive in the middle, its nine specialists
 * seated on a ring around it. The ring breaks around each seat (a mask, so the
 * mark needs no tile behind it and works in any `currentColor`).
 *
 * `consulting` plays the Consult animation (globals.css): a line reaches from
 * the Executive to each specialist in turn and lights it up, then lets go.
 * Used while the Executive is working on a reply; with reduced motion it is
 * the still mark.
 */
export function CouncilMark({
  className = "",
  consulting = false,
}: {
  className?: string;
  consulting?: boolean;
}) {
  const maskId = `council-ring-${useId()}`;
  return (
    <svg
      viewBox="0 0 48 48"
      fill="currentColor"
      className={`${consulting ? "council-consult overflow-visible " : ""}${className}`}
      aria-hidden
    >
      <mask id={maskId} maskUnits="userSpaceOnUse" x="0" y="0" width="48" height="48">
        <rect width="48" height="48" fill="#fff" />
        {SEATS.map(([x, y], i) => (
          <circle key={`m${i}`} cx={x} cy={y} r="3.8" fill="#000" />
        ))}
      </mask>
      <circle
        cx="24"
        cy="24"
        r="14"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        opacity="0.85"
        mask={`url(#${maskId})`}
      />
      {consulting &&
        SEATS.map(([x, y], i) => (
          <line
            key={`l${i}`}
            className="council-spoke"
            x1="24"
            y1="24"
            x2={x}
            y2={y}
            stroke="currentColor"
            style={{ animationDelay: `${i * 0.12}s` }}
          />
        ))}
      {SEATS.map(([x, y], i) => (
        <circle
          key={`s${i}`}
          className={consulting ? "council-seat" : undefined}
          cx={x}
          cy={y}
          r="2.9"
          style={consulting ? { animationDelay: `${i * 0.12 + 0.2}s` } : undefined}
        />
      ))}
      <circle className={consulting ? "council-core" : undefined} cx="24" cy="24" r="6" />
    </svg>
  );
}

interface BrandMarkProps {
  size?: BrandMarkSize;
  /** Play the Consult animation, while the Executive is working on a reply. */
  consulting?: boolean;
}

export default function BrandMark({ size = "sm", consulting = false }: BrandMarkProps) {
  return (
    <div className={BOX_CLASSES[size]} aria-hidden>
      <CouncilMark className={MARK_CLASSES[size]} consulting={consulting} />
    </div>
  );
}
