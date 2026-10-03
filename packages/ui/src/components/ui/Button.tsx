import type { ButtonHTMLAttributes } from "react";

// The app's buttons. One primary per card or screen; everything else is
// secondary or ghost, and rare or destructive actions go in an OverflowMenu.
// `buttonClass` is exported so a <Link> or <a> can look like a button.

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "md" | "sm";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-xl font-semibold whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-50 disabled:cursor-not-allowed";

const SIZES: Record<ButtonSize, string> = {
  md: "h-11 px-5 text-[15px]",
  sm: "h-9 px-3.5 text-sm",
};

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent-strong text-white hover:bg-accent-strong/90 shadow-sm",
  secondary: "bg-surface-overlay text-fg border border-line hover:bg-surface-hover",
  ghost: "text-fg-muted hover:text-fg hover:bg-surface-overlay",
  danger: "text-rose-500 border border-line hover:bg-rose-500/10",
};

export function buttonClass(
  variant: ButtonVariant = "secondary",
  size: ButtonSize = "md",
  extra = "",
): string {
  return `${BASE} ${SIZES[size]} ${VARIANTS[variant]} ${extra}`.trim();
}

export default function Button({
  variant = "secondary",
  size = "md",
  className = "",
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return <button type={type} className={buttonClass(variant, size, className)} {...rest} />;
}
