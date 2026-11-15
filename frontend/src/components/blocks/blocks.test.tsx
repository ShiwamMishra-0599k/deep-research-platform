import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Block } from "@/types";

// jsdom has no layout engine, so the real Leaflet map cannot render. Mock react-leaflet at the module boundary
// and assert OUR logic (accessible name, badge, one marker per place, popup content).
vi.mock("react-leaflet", () => ({
  MapContainer: ({ children, center, zoom }: any) => <div data-testid="map" data-center={JSON.stringify(center)} data-zoom={zoom}>{children}</div>,
  TileLayer: ({ attribution }: any) => <div data-testid="tiles" data-attribution={attribution} />,
  Marker: ({ children, title, position }: any) => <div data-testid="marker" data-title={title} data-pos={JSON.stringify(position)}>{children}</div>,
  Popup: ({ children }: any) => <div data-testid="popup">{children}</div>,
  useMap: () => ({ setView: vi.fn(), fitBounds: vi.fn() }),
}));

import BlockRenderer from "@/components/blocks/BlockRenderer";
import DataTableBlock from "@/components/blocks/DataTableBlock";
import InsightCardsBlock from "@/components/blocks/InsightCardsBlock";
import MarkdownBlock from "@/components/blocks/MarkdownBlock";
import FallbackBlock from "@/components/blocks/FallbackBlock";
import LeafletMapBlock from "@/components/blocks/LeafletMapBlock";

describe("BlockRenderer", () => {
  it("renders nothing for null, undefined or an empty list", () => {
    for (const blocks of [null, undefined, []] as const) {
      const { container, unmount } = render(<BlockRenderer blocks={blocks as never} />);
      expect(container).toBeEmptyDOMElement();
      unmount();
    }
  });

  it("routes each known template_id to its component, in order", () => {
    const blocks: Block[] = [
      { template_id: "markdown", data: { content: "## Heading text" } },
      { template_id: "data-table", data: { columns: ["A"], rows: [{ A: "cell" }] } },
      { template_id: "insight-cards", data: { items: [{ title: "Card title", body: "Price: 5" }] } },
      { template_id: "leaflet-map", data: { center: { lat: 1, lon: 2 }, markers: [{ lat: 1, lon: 2, label: "P" }] } },
    ];
    render(<BlockRenderer blocks={blocks} />);
    expect(screen.getByRole("heading", { name: "Heading text" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Results table" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Card title" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /map showing 1 place/i })).toBeInTheDocument();
  });

  it("INVARIANT: an unknown block type degrades to the JSON fallback instead of breaking the page", async () => {
    render(<BlockRenderer blocks={[{ template_id: "mystery-widget", data: { hello: "world" } }, { template_id: "markdown", data: { content: "still here" } }]} />);
    expect(screen.getByText("mystery-widget")).toBeInTheDocument();
    expect(screen.getByText("still here")).toBeInTheDocument(); // neighbours still render
    expect(screen.queryByText(/"hello"/)).not.toBeInTheDocument(); // collapsed by default
    await userEvent.click(screen.getByRole("button", { name: /unsupported content type/i }));
    expect(screen.getByText(/"hello": "world"/)).toBeInTheDocument();
  });
});

describe("FallbackBlock", () => {
  it("toggles the JSON panel and reflects it in aria-expanded / aria-controls", async () => {
    render(<FallbackBlock template_id="x" data={{ a: 1 }} />);
    const btn = screen.getByRole("button");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(btn.getAttribute("aria-controls")!);
    expect(panel).toHaveTextContent('"a": 1');
    await userEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/"a": 1/)).not.toBeInTheDocument();
  });
});

describe("DataTableBlock", () => {
  const rows = [
    { Name: "Bravo", Distance: "10km", Site: "https://example.com/b" },
    { Name: "alpha", Distance: "2km", Site: null },
    { Name: "Charlie", Distance: "30km", Site: "plain text" },
  ];
  const names = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent);

  it("renders headers and one row per record in the original order", () => {
    render(<DataTableBlock data={{ columns: ["Name", "Distance", "Site"], rows }} />);
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent?.replace(/, sort/, ""))).toEqual(["Name", "Distance", "Site"]);
    expect(names()).toEqual(["Bravo", "alpha", "Charlie"]);
  });

  it("sorts text case-insensitively, toggles direction, and exposes it via aria-sort", async () => {
    render(<DataTableBlock data={{ columns: ["Name", "Distance"], rows }} />);
    const nameHeader = screen.getAllByRole("columnheader")[0];
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    await userEvent.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending");
    expect(names()).toEqual(["alpha", "Bravo", "Charlie"]);
    await userEvent.click(within(nameHeader).getByRole("button"));
    expect(nameHeader).toHaveAttribute("aria-sort", "descending");
    expect(names()).toEqual(["Charlie", "Bravo", "alpha"]);
  });

  it("sorts numerically when values are numeric-prefixed (2km before 10km, not '10' < '2')", async () => {
    render(<DataTableBlock data={{ columns: ["Name", "Distance"], rows }} />);
    await userEvent.click(within(screen.getAllByRole("columnheader")[1]).getByRole("button"));
    expect(names()).toEqual(["alpha", "Bravo", "Charlie"]); // 2, 10, 30
  });

  it("switching to another column resets to ascending and clears the previous aria-sort", async () => {
    render(<DataTableBlock data={{ columns: ["Name", "Distance"], rows }} />);
    const [h1, h2] = screen.getAllByRole("columnheader");
    await userEvent.click(within(h1).getByRole("button"));
    await userEvent.click(within(h1).getByRole("button")); // descending
    await userEvent.click(within(h2).getByRole("button"));
    expect(h2).toHaveAttribute("aria-sort", "ascending");
    expect(h1).toHaveAttribute("aria-sort", "none");
  });

  it("http(s) cells become safe external links; null cells show an em dash; other text is plain", () => {
    render(<DataTableBlock data={{ columns: ["Name", "Site"], rows }} />);
    const link = screen.getByRole("link", { name: "example.com/b" });
    expect(link).toHaveAttribute("href", "https://example.com/b");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getByLabelText("No value")).toHaveTextContent("—");
    expect(screen.getByText("plain text").closest("a")).toBeNull();
  });

  it("a javascript: URL in a cell is NOT turned into a link", () => {
    render(<DataTableBlock data={{ columns: ["Site"], rows: [{ Site: "javascript:alert(1)" }] }} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
  });

  it("shows 'No rows' for an empty table and tolerates missing columns/rows", () => {
    const { rerender } = render(<DataTableBlock data={{ columns: ["A"], rows: [] }} />);
    expect(screen.getByText("No rows")).toBeInTheDocument();
    rerender(<DataTableBlock data={{} as never} />);
    expect(screen.getByText("No rows")).toBeInTheDocument();
  });

  it("the scroll area is keyboard-focusable so wide tables can be scrolled without a mouse", () => {
    render(<DataTableBlock data={{ columns: ["A"], rows: [{ A: "x" }] }} />);
    expect(screen.getByRole("region", { name: "Results table" })).toHaveAttribute("tabindex", "0");
  });
});

describe("InsightCardsBlock", () => {
  it("splits 'Label: value · Label: value' into label/value pairs", () => {
    render(<InsightCardsBlock data={{ items: [{ title: "RELIANCE Stock Price", body: "Price: 2890.4 · Change: -1.12% · 52w: 2220 – 3024", severity: "warning" }] }} />);
    const card = screen.getByRole("listitem");
    const terms = within(card).getAllByRole("term").map((t) => t.textContent);
    const defs = within(card).getAllByRole("definition").map((d) => d.textContent);
    expect(terms).toEqual(["Price", "Change", "52w"]);
    expect(defs).toEqual(["2890.4", "-1.12%", "2220 – 3024"]);
  });

  it("renders segments without a 'Label:' as plain lines", () => {
    render(<InsightCardsBlock data={{ items: [{ title: "Bitcoin Price", body: "₹7140000 · $85210 · 24h: +2.31%", severity: "success" }] }} />);
    const card = screen.getByRole("listitem");
    expect(within(card).getByText("₹7140000")).toBeInTheDocument();
    expect(within(card).getByText("$85210")).toBeInTheDocument();
    expect(within(card).getByRole("term")).toHaveTextContent("24h");
  });

  it("conveys severity with a TEXT label (not colour alone) and defaults to Info", () => {
    render(<InsightCardsBlock data={{ items: [
      { title: "a", body: "x", severity: "success" }, { title: "b", body: "x", severity: "warning" }, { title: "c", body: "x" },
    ] }} />);
    const cards = screen.getAllByRole("listitem");
    expect(within(cards[0]).getByText("Up")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Caution")).toBeInTheDocument();
    expect(within(cards[2]).getByText("Info")).toBeInTheDocument();
  });

  it("only treats short prefixes as labels (a long sentence containing a colon is not a pair)", () => {
    render(<InsightCardsBlock data={{ items: [{ title: "t", body: "This is a very long sentence that happens to contain: a colon" }] }} />);
    expect(screen.queryByRole("term")).not.toBeInTheDocument();
  });

  it("renders no cards for an empty or missing item list", () => {
    const { rerender } = render(<InsightCardsBlock data={{ items: [] }} />);
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    rerender(<InsightCardsBlock data={{} as never} />);
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
});

describe("MarkdownBlock", () => {
  it("renders GFM: headings, lists, bold", () => {
    render(<MarkdownBlock data={{ content: "## Title\n\n- one\n- two\n\n**bold**" }} />);
    expect(screen.getByRole("heading", { level: 2, name: "Title" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("wraps tables in a focusable, labelled scroll region", () => {
    render(<MarkdownBlock data={{ content: "| A | B |\n|---|---|\n| 1 | 2 |" }} />);
    const region = screen.getByRole("region", { name: "Table" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(within(region).getByRole("table")).toBeInTheDocument();
  });

  it("REGRESSION GUARD: every link opens in a new tab with rel=noreferrer", () => {
    render(<MarkdownBlock data={{ content: "[docs](https://example.com/page)" }} />);
    const a = screen.getByRole("link", { name: "docs" });
    expect(a).toHaveAttribute("href", "https://example.com/page");
    expect(a).toHaveAttribute("target", "_blank");
    expect(a).toHaveAttribute("rel", "noreferrer");
  });

  it("XSS GUARD: raw HTML from the model is shown as text, never as live elements", () => {
    const { container } = render(<MarkdownBlock data={{ content: '<img src=x onerror="window.__xss=1"> <script>window.__xss=2</script>' }} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect((window as any).__xss).toBeUndefined();
  });

  it("XSS GUARD: a javascript: link target is neutralised", () => {
    render(<MarkdownBlock data={{ content: "[click](javascript:alert(1))" }} />);
    const a = screen.queryByRole("link", { name: "click" });
    expect(a?.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
  });

  it("renders an empty string without crashing", () => {
    const { container } = render(<MarkdownBlock data={{ content: "" }} />);
    expect(container.querySelector(".markdown-body")).toBeEmptyDOMElement();
  });
});

describe("LeafletMapBlock (react-leaflet mocked: jsdom has no layout)", () => {
  const data = {
    center: { lat: 26.8, lon: 80.9 },
    zoom: 11,
    markers: [
      { lat: 26.8, lon: 80.9, label: "Tata Power", popup: "Gomti Nagar" },
      { lat: 26.9, lon: 80.8, label: "Statiq" },
    ],
  };

  it("is a labelled region with the place count and one marker per place", () => {
    render(<LeafletMapBlock data={data} />);
    expect(screen.getByRole("region", { name: "Map showing 2 places" })).toBeInTheDocument();
    expect(screen.getByText("2 places")).toBeInTheDocument();
    expect(screen.getAllByTestId("marker").map((m) => m.dataset.title)).toEqual(["Tata Power", "Statiq"]);
  });

  it("passes the centre and zoom to the map, and puts popup text only where given", () => {
    render(<LeafletMapBlock data={data} />);
    expect(JSON.parse(screen.getByTestId("map").dataset.center!)).toEqual([26.8, 80.9]);
    expect(screen.getByTestId("map").dataset.zoom).toBe("11");
    expect(screen.getAllByTestId("popup")[0]).toHaveTextContent("Tata Power");
    expect(screen.getAllByTestId("popup")[0]).toHaveTextContent("Gomti Nagar");
    expect(screen.getAllByTestId("popup")[1]).toHaveTextContent("Statiq");
  });

  it("uses singular wording for one place and shows no count badge when there are none", () => {
    const { rerender } = render(<LeafletMapBlock data={{ ...data, markers: [data.markers[0]] }} />);
    expect(screen.getByRole("region", { name: "Map showing 1 place" })).toBeInTheDocument();
    rerender(<LeafletMapBlock data={{ center: { lat: 0, lon: 0 }, markers: [] }} />);
    expect(screen.queryByText(/places?$/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Map showing 0 places" })).toBeInTheDocument();
  });

  it("falls back to a sensible default zoom and a (0,0) centre when data is missing", () => {
    render(<LeafletMapBlock data={{ markers: [] } as never} />);
    expect(screen.getByTestId("map").dataset.zoom).toBe("13");
    expect(JSON.parse(screen.getByTestId("map").dataset.center!)).toEqual([0, 0]);
  });
});
