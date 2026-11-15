import type { ReactNode } from "react";
import Logo from "./Logo";

const POINTS = [
  "Every answer links to its sources.",
  "Live prices, news and nearby places.",
  "Conversations are saved to your account.",
];

/** Shared frame for sign-in and registration: charcoal brand panel (lg+) beside the ivory form. */
export default function AuthLayout({
  title,
  subtitle,
  footer,
  children,
}: {
  title: string;
  subtitle: string;
  footer: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="min-h-full flex flex-col lg:flex-row bg-canvas">
      <aside className="on-nav hidden lg:flex lg:w-[26rem] xl:w-[30rem] shrink-0 flex-col justify-between bg-nav text-nav-text p-10">
        <div className="flex items-center gap-2.5">
          <Logo onDark />
          <span className="text-lg font-semibold">Lumen</span>
        </div>
        <div>
          <p className="text-xl font-semibold leading-snug">A research workspace that shows its work.</p>
          <ul className="mt-6 space-y-3 text-sm text-nav-muted">
            {POINTS.map((p) => (
              <li key={p} className="flex gap-3">
                <span aria-hidden="true" className="mt-2 h-1 w-1 shrink-0 rounded-full bg-accent-on-dark" />
                {p}
              </li>
            ))}
          </ul>
        </div>
        <span aria-hidden="true" />
      </aside>

      <main className="flex-1 grid place-items-center px-4 py-10 sm:px-6">
        <div className="w-full max-w-sm">
          <div className="flex items-center gap-2.5 mb-8 lg:hidden">
            <Logo />
            <span className="text-lg font-semibold">Lumen</span>
          </div>
          <h1 className="text-xl font-semibold">{title}</h1>
          <p className="mt-1 text-sm text-muted">{subtitle}</p>
          <div className="mt-6">{children}</div>
          <p className="mt-6 text-sm text-muted">{footer}</p>
        </div>
      </main>
    </div>
  );
}
