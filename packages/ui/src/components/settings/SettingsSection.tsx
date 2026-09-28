import type { ReactNode } from "react";

// One section of the Settings page: a heading the in-page nav can land on
// (`id` is the hash), an optional one-line description, and a card body.
// `scroll-mt` keeps the heading clear of the sticky chip row that sits at
// the top of the scroller below `lg`; on `lg` nothing overlaps it.
export default function SettingsSection({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  const titleId = `${id}-title`;
  return (
    <section id={id} aria-labelledby={titleId} className="scroll-mt-16 lg:scroll-mt-6">
      <h2 id={titleId} className="text-sm font-medium text-fg">
        {title}
      </h2>
      {description && <p className="mt-1 text-xs text-fg-muted leading-relaxed">{description}</p>}
      <div className="mt-3 rounded-xl border border-line bg-surface-elevated p-4">{children}</div>
    </section>
  );
}
