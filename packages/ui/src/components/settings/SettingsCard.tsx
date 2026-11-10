import { Children, type ReactNode } from "react";

// One card on a Settings page: an optional title and line of explanation,
// then the controls. `titleId` lets a switch in the card name itself by the
// card's title. `tone="accent"` draws it with the accent border, for the one
// setting a page wants seen first (Delegate → As you's In training).
export default function SettingsCard({
  tone,
  title,
  titleId,
  description,
  action,
  children,
}: {
  tone?: "accent";
  title?: ReactNode;
  titleId?: string;
  description?: ReactNode;
  /** A control beside the title, such as a switch. */
  action?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section
      className={`rounded-2xl bg-surface-elevated p-5 sm:p-6 ${tone === "accent" ? "border-2 border-accent" : "border border-line"}`}
    >
      {(title || action) && (
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            {title && (
              <h2 id={titleId} className="text-base sm:text-lg font-semibold text-fg">
                {title}
              </h2>
            )}
            {description && (
              <p className="mt-1 text-sm text-fg-muted leading-relaxed">{description}</p>
            )}
          </div>
          {action && <div className="flex-shrink-0 pt-1">{action}</div>}
        </div>
      )}
      {Children.toArray(children).length > 0 && (
        <div className={title || action ? "mt-4" : ""}>{children}</div>
      )}
    </section>
  );
}
