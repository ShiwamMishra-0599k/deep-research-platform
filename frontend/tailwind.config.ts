import type { Config } from "tailwindcss";

/**
 * Tailwind only *references* the CSS variables in src/styles/tokens.css.
 * Add or change a value there, never here.
 */
const rgb = (name: string) => `rgb(var(--c-${name}) / <alpha-value>)`;

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // Explicit breakpoints (documented in DESIGN.md)
    screens: {
      sm: "640px",   // large phone / small tablet
      md: "768px",   // tablet
      lg: "1024px",  // laptop — sidebar becomes persistent
      xl: "1280px",  // desktop
    },
    // Replace (not extend) so off-palette colours cannot creep in.
    colors: {
      transparent: "transparent",
      current: "currentColor",
      white: "#ffffff", // reserved for text on accent/danger fills
      canvas: rgb("canvas"),
      surface: rgb("surface"),
      sunken: rgb("sunken"),
      nav: {
        DEFAULT: rgb("nav"),
        raised: rgb("nav-raised"),
        line: rgb("nav-line"),
        text: rgb("nav-text"),
        muted: rgb("nav-muted"),
      },
      accent: {
        DEFAULT: rgb("accent"),
        strong: rgb("accent-strong"),
        deep: rgb("accent-deep"),
        tint: rgb("accent-tint"),
        "on-dark": rgb("accent-on-dark"),
      },
      ink: rgb("ink"),
      muted: rgb("muted"),
      placeholder: rgb("placeholder"),
      line: { DEFAULT: rgb("line"), strong: rgb("line-strong") },
      success: { DEFAULT: rgb("success"), tint: rgb("success-tint") },
      warning: { DEFAULT: rgb("warning"), tint: rgb("warning-tint") },
      danger: { DEFAULT: rgb("danger"), tint: rgb("danger-tint"), "on-dark": rgb("danger-on-dark") },
    },
    fontFamily: {
      sans: ["var(--font-sans)"],
      mono: ["var(--font-mono)"],
    },
    // Type scale: meta / UI / body / reading + three heading sizes. Weights: 400, 500, 600 only.
    fontSize: {
      xs: ["0.75rem", { lineHeight: "1rem" }],        // 12 — metadata
      sm: ["0.875rem", { lineHeight: "1.25rem" }],    // 14 — UI text
      base: ["1rem", { lineHeight: "1.625rem" }],     // 16 — reading text
      lg: ["1.125rem", { lineHeight: "1.75rem" }],    // 18 — section heading
      xl: ["1.5rem", { lineHeight: "2rem" }],         // 24 — page heading
    },
    fontWeight: { normal: "400", medium: "500", semibold: "600" },
    borderRadius: {
      none: "0",
      control: "var(--radius-control)",
      card: "var(--radius-card)",
      full: "9999px", // avatars / spinners only
    },
    boxShadow: {
      none: "none",
      sm: "var(--shadow-sm)",
      md: "var(--shadow-md)",
    },
    extend: {
      maxWidth: { content: "var(--content-width)" },
      width: { sidebar: "var(--sidebar-width)" },
      height: { header: "var(--header-height)" },
      minHeight: { header: "var(--header-height)" },
      ringOffsetColor: { canvas: rgb("canvas") },
    },
  },
  plugins: [],
};

export default config;
