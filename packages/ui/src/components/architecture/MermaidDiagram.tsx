'use client';

import { useEffect, useState } from 'react';

interface Props {
  definition: string;
  id: string;
}

// Shared classDef rules appended to every diagram so node *roles* are visually
// distinct across all diagrams. Each role corresponds to a chip in the
// architecture page's <DiagramLegend />.
const SHARED_CLASSDEFS = `
classDef entry fill:#1e3a8a,stroke:#60a5fa,stroke-width:1.5px,color:#dbeafe
classDef compute fill:#312e81,stroke:#a5b4fc,stroke-width:1.5px,color:#e0e7ff
classDef storage fill:#365314,stroke:#a3e635,stroke-width:1.5px,color:#ecfccb
classDef cache fill:#713f12,stroke:#facc15,stroke-width:1.5px,color:#fef3c7
classDef external fill:#3f3f46,stroke:#a1a1aa,stroke-width:1.5px,color:#fafafa
classDef hot fill:#7f1d1d,stroke:#fca5a5,stroke-width:1.5px,color:#fecaca
`;

function withSharedStyles(definition: string): string {
  // classDef is flowchart-only — sequence diagrams reject it.
  const trimmed = definition.trim();
  if (trimmed.startsWith('sequenceDiagram')) return definition;
  return `${definition}\n${SHARED_CLASSDEFS}`;
}

export default function MermaidDiagram({ definition, id }: Props) {
  // Holding the SVG in React state (instead of mutating the container's
  // innerHTML directly) is what lets React safely re-render this
  // component on regenerate. Prior implementations mutated the DOM
  // outside React's knowledge, which caused
  // `NotFoundError: Failed to execute 'removeChild' on 'Node'` when
  // React later tried to reconcile a child it hadn't inserted.
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function render() {
      try {
        const mermaid = (await import('mermaid')).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: 'dark',
          darkMode: true,
          securityLevel: 'loose', // required for <br/> in node labels
          flowchart: {
            padding: 24,
            nodeSpacing: 60,
            rankSpacing: 80,
            curve: 'basis',
            useMaxWidth: true,
            htmlLabels: true,
          },
          sequence: {
            actorMargin: 70,
            boxMargin: 14,
            messageMargin: 45,
            mirrorActors: false,
          },
          themeVariables: {
            background: 'transparent',
            // Bumped from 14px so labels are readable inside dense flowcharts.
            fontSize: '15px',
            primaryColor: '#1e293b',
            primaryTextColor: '#f4f4f5',
            primaryBorderColor: '#a5b4fc',
            lineColor: '#a1a1aa',
            secondaryColor: '#27272a',
            tertiaryColor: '#3f3f46',
            clusterBkg: '#18181b',
            clusterBorder: '#52525b',
            edgeLabelBackground: '#18181b',
            // Sequence diagram specifics
            actorBkg: '#1e3a8a',
            actorBorder: '#60a5fa',
            actorTextColor: '#dbeafe',
            actorLineColor: '#71717a',
            signalColor: '#d4d4d8',
            signalTextColor: '#f4f4f5',
            labelBoxBkgColor: '#312e81',
            labelBoxBorderColor: '#a5b4fc',
            labelTextColor: '#e0e7ff',
            noteBkgColor: '#365314',
            noteBorderColor: '#a3e635',
            noteTextColor: '#ecfccb',
          },
        });

        // mermaid.render appends a temporary <div id="…"> to <body> while
        // rendering. Use a uniqueid per call so concurrent renders on a
        // remount don't collide.
        const uniqueId = `mermaid-${id}-${Math.random().toString(36).slice(2, 10)}`;
        const result = await mermaid.render(uniqueId, withSharedStyles(definition));
        if (!cancelled) {
          setSvg(result.svg);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : 'Diagram render failed';
          // Strip the noisy "Parse error on line N" stack — keep only the
          // first line so the inline message stays compact.
          setError(msg.split('\n')[0]);
          setSvg(null);
          // Keep the console error for devs; users see the inline fallback.
           
          console.error('Mermaid render failed:', e);
        }
      }
    }

    render();
    return () => {
      cancelled = true;
    };
  }, [definition, id]);

  if (error) {
    return (
      <div className="rounded-lg bg-surface border border-line px-4 py-3 text-xs text-fg-muted">
        <span className="font-medium text-fg-muted">Diagram unavailable.</span>{' '}
        <span className="text-fg-muted">
          This diagram could not be rendered.
        </span>
      </div>
    );
  }

  if (!svg) {
    return (
      <div
        className="rounded-lg bg-surface-elevated border border-line p-6 text-xs text-fg-subtle animate-pulse"
        style={{ minHeight: 200 }}
      >
        Rendering diagram…
      </div>
    );
  }

  // dangerouslySetInnerHTML lets React track this subtree so unmount /
  // remount during regenerate doesn't double-remove DOM nodes.
  return (
    <div
      className="rounded-lg bg-surface-elevated border border-line p-6 overflow-x-auto [&_svg]:max-w-full [&_svg]:h-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
