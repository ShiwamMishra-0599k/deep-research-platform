/** Lumen mark: a ring and a point of light. `onDark` is for charcoal surfaces. */
export default function Logo({ size = 28, onDark = false }: { size?: number; onDark?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="shrink-0">
      <rect
        width="32" height="32" rx="7"
        style={{ fill: onDark ? "rgb(var(--c-nav-raised))" : "rgb(var(--c-nav))" }}
      />
      <circle
        cx="16" cy="16" r="7" strokeWidth="2.4"
        style={{ fill: "none", stroke: "rgb(var(--c-accent-on-dark))" }}
      />
      <circle cx="16" cy="16" r="2.6" style={{ fill: "rgb(var(--c-nav-text))" }} />
    </svg>
  );
}
