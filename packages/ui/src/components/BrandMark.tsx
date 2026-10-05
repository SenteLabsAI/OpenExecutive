type BrandMarkSize = "sm" | "md" | "lg";

const BOX_CLASSES: Record<BrandMarkSize, string> = {
  sm: "w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-lg shadow-indigo-500/20",
  md: "w-7 h-7 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-lg shadow-indigo-500/20",
  lg: "w-12 h-12 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center shadow-xl shadow-indigo-500/25",
};

const MARK_CLASSES: Record<BrandMarkSize, string> = {
  sm: "w-[18px] h-[18px] text-white",
  md: "w-4 h-4 text-white",
  lg: "w-8 h-8 text-white",
};

// The five specialists' seats around the Executive at (24, 24).
const SEATS: ReadonlyArray<readonly [number, number]> = [
  [24, 6.5],
  [40.6, 18.6],
  [34.3, 38.2],
  [13.7, 38.2],
  [7.4, 18.6],
];

/**
 * The Council mark alone: the Executive in the middle, five specialists around it.
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
  return (
    <svg
      viewBox="0 0 48 48"
      fill="currentColor"
      className={`${consulting ? "council-consult overflow-visible " : ""}${className}`}
      aria-hidden
    >
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
            style={{ animationDelay: `${i * 0.18}s` }}
          />
        ))}
      {SEATS.map(([x, y], i) => (
        <circle
          key={`s${i}`}
          className={consulting ? "council-seat" : undefined}
          cx={x}
          cy={y}
          r="4"
          style={consulting ? { animationDelay: `${i * 0.18 + 0.25}s` } : undefined}
        />
      ))}
      <circle className={consulting ? "council-core" : undefined} cx="24" cy="24" r="7.5" />
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
