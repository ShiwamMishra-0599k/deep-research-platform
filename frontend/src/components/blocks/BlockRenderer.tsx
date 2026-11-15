import type { Block, DataTableData, InsightCardsData, LeafletMapData, MarkdownData } from "@/types";
import MarkdownBlock from "./MarkdownBlock";
import DataTableBlock from "./DataTableBlock";
import InsightCardsBlock from "./InsightCardsBlock";
import LeafletMapBlock from "./LeafletMapBlock";
import FallbackBlock from "./FallbackBlock";

function renderOne(b: Block) {
  switch (b.template_id) {
    case "markdown":
      return <MarkdownBlock data={b.data as MarkdownData} />;
    case "data-table":
      return <DataTableBlock data={b.data as DataTableData} />;
    case "insight-cards":
      return <InsightCardsBlock data={b.data as InsightCardsData} />;
    case "leaflet-map":
      return <LeafletMapBlock data={b.data as LeafletMapData} />;
    default:
      // Unknown block types never break the page; they degrade to a JSON viewer.
      return <FallbackBlock template_id={b.template_id} data={b.data} />;
  }
}

export default function BlockRenderer({ blocks }: { blocks: Block[] | null | undefined }) {
  if (!blocks?.length) return null;
  return (
    <div className="space-y-5">
      {blocks.map((b, i) => (
        <div key={i}>{renderOne(b)}</div>
      ))}
    </div>
  );
}
