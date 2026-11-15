import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import type { DataTableData } from "@/types";

export default function DataTableBlock({ data }: { data: DataTableData }) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [dir, setDir] = useState<"asc" | "desc">("asc");

  const columns = data.columns ?? [];
  const rows = data.rows ?? [];

  const sorted = useMemo(() => {
    if (!sortKey) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = String(a[sortKey] ?? "").toLowerCase();
      const bv = String(b[sortKey] ?? "").toLowerCase();
      const an = parseFloat(av), bn = parseFloat(bv);
      const both = !isNaN(an) && !isNaN(bn);
      const cmp = both ? an - bn : av.localeCompare(bv);
      return dir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, dir]);

  function toggleSort(col: string) {
    if (sortKey === col) setDir(dir === "asc" ? "desc" : "asc");
    else { setSortKey(col); setDir("asc"); }
  }

  function renderCell(v: unknown) {
    if (v == null || v === "") return <span className="text-muted" aria-label="No value">—</span>;
    if (typeof v === "string" && /^https?:\/\//.test(v)) {
      return (
        <a href={v} target="_blank" rel="noreferrer"
           className="text-accent-strong underline underline-offset-2 hover:text-accent-deep truncate block max-w-[28ch]">
          {v.replace(/^https?:\/\//, "")}
        </a>
      );
    }
    return String(v);
  }

  return (
    <div className="card overflow-hidden">
      {/* Scrolls inside the card on narrow screens; focusable so keyboard users can scroll it. */}
      <div className="overflow-auto max-h-[28rem]" tabIndex={0} role="region" aria-label="Results table">
        <table className="w-full text-sm border-separate border-spacing-0">
          <thead className="sticky top-0 z-10 bg-sunken">
            <tr>
              {columns.map((c) => {
                const sorted = sortKey === c;
                return (
                  <th
                    key={c} scope="col"
                    aria-sort={sorted ? (dir === "asc" ? "ascending" : "descending") : "none"}
                    className="text-left font-medium px-4 py-2.5 border-b border-line whitespace-nowrap"
                  >
                    <button
                      type="button" onClick={() => toggleSort(c)}
                      className="inline-flex items-center gap-1.5 -mx-1 px-1 rounded-control hover:text-accent-deep"
                    >
                      {c}
                      {sorted
                        ? (dir === "asc" ? <ArrowUp size={12} aria-hidden="true" /> : <ArrowDown size={12} aria-hidden="true" />)
                        : <ArrowUpDown size={12} aria-hidden="true" className="text-muted" />}
                      <span className="sr-only">, sort</span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody className="[&>tr:last-child>td]:border-b-0">
            {sorted.map((row, i) => (
              <tr key={i} className="hover:bg-sunken/60 transition-colors">
                {columns.map((c) => (
                  <td key={c} className="px-4 py-2.5 align-top border-b border-line tabular-nums">
                    {renderCell(row[c])}
                  </td>
                ))}
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr><td colSpan={columns.length || 1} className="px-4 py-8 text-center text-muted">No rows</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
