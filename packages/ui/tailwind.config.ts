import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

// rgb(var(--token) / <alpha-value>) lets Tailwind apply opacity modifiers
// like `bg-surface-overlay/60` to CSS-variable colors. The CSS vars in
// globals.css are space-separated RGB triplets to support this.
const rgbVar = (name: string) => `rgb(var(${name}) / <alpha-value>)`;

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: rgbVar("--surface-base"),
          base: rgbVar("--surface-base"),
          elevated: rgbVar("--surface-elevated"),
          overlay: rgbVar("--surface-overlay"),
          input: rgbVar("--surface-input"),
          hover: rgbVar("--surface-hover"),
        },
        fg: {
          DEFAULT: rgbVar("--fg"),
          muted: rgbVar("--fg-muted"),
          subtle: rgbVar("--fg-subtle"),
          inverse: rgbVar("--fg-inverse"),
        },
        line: {
          DEFAULT: rgbVar("--border-default"),
          strong: rgbVar("--border-strong"),
          subtle: rgbVar("--border-subtle"),
        },
        accent: {
          DEFAULT: rgbVar("--accent"),
          strong: rgbVar("--accent-strong"),
        },
      },
      borderColor: {
        DEFAULT: rgbVar("--border-default"),
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        display: ["var(--font-display)", "var(--font-sans)", "system-ui", "sans-serif"],
      },
      minHeight: {
        touch: "2.5rem",
      },
      minWidth: {
        touch: "2.5rem",
      },
      typography: {
        DEFAULT: {
          css: {
            "--tw-prose-body": rgbVar("--fg-muted"),
            "--tw-prose-headings": rgbVar("--fg"),
            "--tw-prose-bold": rgbVar("--fg"),
            "--tw-prose-code": rgbVar("--accent"),
            "--tw-prose-links": rgbVar("--accent"),
            "--tw-prose-bullets": rgbVar("--fg-subtle"),
            "--tw-prose-counters": rgbVar("--fg-subtle"),
            "--tw-prose-quotes": rgbVar("--fg-muted"),
            "--tw-prose-quote-borders": rgbVar("--border-strong"),
            "--tw-prose-hr": rgbVar("--border-default"),
            "--tw-prose-th-borders": rgbVar("--border-strong"),
            "--tw-prose-td-borders": rgbVar("--border-default"),
          },
        },
        invert: {
          css: {
            "--tw-prose-body": rgbVar("--fg-muted"),
            "--tw-prose-headings": rgbVar("--fg"),
            "--tw-prose-bold": rgbVar("--fg"),
            "--tw-prose-code": rgbVar("--accent"),
            "--tw-prose-links": rgbVar("--accent"),
            "--tw-prose-bullets": rgbVar("--fg-subtle"),
            "--tw-prose-counters": rgbVar("--fg-subtle"),
            "--tw-prose-quotes": rgbVar("--fg-muted"),
            "--tw-prose-quote-borders": rgbVar("--border-strong"),
            "--tw-prose-hr": rgbVar("--border-default"),
            "--tw-prose-th-borders": rgbVar("--border-strong"),
            "--tw-prose-td-borders": rgbVar("--border-default"),
          },
        },
      },
    },
  },
  plugins: [typography],
};

export default config;
