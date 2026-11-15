import { useId, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";

export default function FallbackBlock({ template_id, data }: { template_id: string; data: unknown }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  return (
    <div className="card p-3">
      <button
        type="button" onClick={() => setOpen((o) => !o)}
        aria-expanded={open} aria-controls={panelId}
        className="flex items-center gap-2 text-sm text-muted hover:text-ink"
      >
        Unsupported content type <code className="font-mono text-xs bg-sunken px-1.5 py-0.5 rounded-control">{template_id}</code>
        {open ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
      </button>
      {open && (
        <pre id={panelId} className="mt-3 text-xs bg-sunken border border-line rounded-control p-3 overflow-auto max-h-96">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}
