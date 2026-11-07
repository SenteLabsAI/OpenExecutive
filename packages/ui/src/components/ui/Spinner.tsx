// The one loading mark: the small spinning ring the chat's working line uses
// (TurnSteps.tsx), with no words beside it. Screen readers still hear what is
// loading through the label. `Spinner` sits inline (a count, a button, a
// line); `PageSpinner` centers a slightly bigger ring where a page or panel
// will appear.

const RING = "inline-block flex-shrink-0 rounded-full border-2 border-accent border-r-transparent animate-spin";

export function Spinner({
  label = "Loading",
  size = "sm",
  className = "",
}: {
  label?: string;
  size?: "sm" | "md";
  className?: string;
}) {
  const box = size === "md" ? "w-5 h-5" : "w-3 h-3";
  return (
    <span role="status" className={`inline-flex items-center align-middle ${className}`}>
      <span aria-hidden="true" className={`${RING} ${box}`} />
      <span className="sr-only">{label}</span>
    </span>
  );
}

export function PageSpinner({ label = "Loading", className = "" }: { label?: string; className?: string }) {
  return (
    <div className={`flex justify-center py-10 ${className}`}>
      <Spinner label={label} size="md" />
    </div>
  );
}
