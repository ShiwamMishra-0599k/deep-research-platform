import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { MarkdownData } from "@/types";

export default function MarkdownBlock({ data }: { data: MarkdownData }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Wide tables scroll inside their own box instead of widening the page.
          table: ({ node, ...props }) => (
            <div className="table-scroll" tabIndex={0} role="region" aria-label="Table">
              <table {...props} />
            </div>
          ),
          a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {data.content || ""}
      </ReactMarkdown>
    </div>
  );
}
