import { Info, AlertTriangle, CheckCircle2 } from "lucide-react";
import type { InsightCardsData, InsightItem } from "@/types";

type Severity = NonNullable<InsightItem["severity"]>;

// Severity is conveyed by icon + label as well as colour, so it never relies on colour alone.
const SEVERITY: Record<Severity, { label: string; icon: JSX.Element; tone: string }> = {
  info:    { label: "Info",    icon: <Info size={14} aria-hidden="true" />,          tone: "text-muted" },
  success: { label: "Up",      icon: <CheckCircle2 size={14} aria-hidden="true" />,  tone: "text-success" },
  warning: { label: "Caution", icon: <AlertTriangle size={14} aria-hidden="true" />, tone: "text-warning" },
};

/** Bodies arrive as "Label: value · Label: value". Show those as aligned rows, anything else as plain lines. */
function BodyRows({ body }: { body: string }) {
  const parts = body.split(" · ").map((s) => s.trim()).filter(Boolean);
  return (
    <dl className="mt-2 space-y-1 text-sm">
      {parts.map((p, i) => {
        const m = p.match(/^([^:]{1,24}):\s*(.+)$/);
        return m ? (
          <div key={i} className="flex justify-between gap-3">
            <dt className="text-muted">{m[1]}</dt>
            <dd className="text-right tabular-nums">{m[2]}</dd>
          </div>
        ) : (
          <div key={i} className="tabular-nums">{p}</div>
        );
      })}
    </dl>
  );
}

export default function InsightCardsBlock({ data }: { data: InsightCardsData }) {
  const items = data.items ?? [];

  return (
    <ul className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
      {items.map((item, i) => {
        const sev = SEVERITY[item.severity || "info"];
        return (
          <li key={i} className="card p-4 min-w-0">
            <div className={`flex items-center gap-1.5 text-xs font-medium ${sev.tone}`}>
              {sev.icon}
              <span>{sev.label}</span>
            </div>
            <h3 className="mt-1.5 text-base font-semibold break-words">{item.title}</h3>
            <BodyRows body={item.body} />
          </li>
        );
      })}
    </ul>
  );
}
