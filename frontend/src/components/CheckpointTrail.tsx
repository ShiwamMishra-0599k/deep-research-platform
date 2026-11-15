import { motion, AnimatePresence } from "framer-motion";
import { Check, Loader2 } from "lucide-react";
import type { Checkpoint } from "@/types";

function fmtElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.floor((ms % 60_000) / 1000)}s`;
}

/** Live research progress. Deliberately quiet: one muted line per step, a single spinner on the current one. */
export default function CheckpointTrail({ items, active }: { items: Checkpoint[]; active: boolean }) {
  if (items.length === 0) return null;
  const first = items[0]?.ts ?? Date.now();

  return (
    <section aria-label="Research progress" aria-live="polite" className="card px-3.5 py-3 mb-4">
      <h3 className="meta font-medium mb-2">
        {active ? "Researching" : "Research steps"}
        <span className="font-normal"> · {items.length} step{items.length === 1 ? "" : "s"}</span>
      </h3>
      <ol className="space-y-1.5">
        <AnimatePresence initial={false}>
          {items.map((c, i) => {
            const inProgress = active && i === items.length - 1;
            const elapsed = c.ts ? c.ts - first : null;
            return (
              <motion.li
                key={i}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.15 }}
                className="flex items-start gap-2.5 text-sm"
              >
                <span className="mt-0.5 h-4 w-4 shrink-0 grid place-items-center text-accent-strong" aria-hidden="true">
                  {inProgress ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                </span>
                <span className={`flex-1 min-w-0 break-words ${inProgress ? "text-ink" : "text-muted"}`}>{c.content}</span>
                {elapsed !== null && (
                  <span className="meta tabular-nums shrink-0">+{fmtElapsed(elapsed)}</span>
                )}
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ol>
    </section>
  );
}
