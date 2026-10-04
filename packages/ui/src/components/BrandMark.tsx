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

/** The Council mark alone: the Executive in the middle, five specialists around it. */
export function CouncilMark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 48 48" fill="currentColor" className={className} aria-hidden>
      <circle cx="24" cy="24" r="7.5" />
      <circle cx="24" cy="6.5" r="4" />
      <circle cx="40.6" cy="18.6" r="4" />
      <circle cx="34.3" cy="38.2" r="4" />
      <circle cx="13.7" cy="38.2" r="4" />
      <circle cx="7.4" cy="18.6" r="4" />
    </svg>
  );
}

interface BrandMarkProps {
  size?: BrandMarkSize;
}

export default function BrandMark({ size = "sm" }: BrandMarkProps) {
  return (
    <div className={BOX_CLASSES[size]} aria-hidden>
      <CouncilMark className={MARK_CLASSES[size]} />
    </div>
  );
}
