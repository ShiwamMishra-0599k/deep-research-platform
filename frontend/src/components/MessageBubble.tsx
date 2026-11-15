import { motion } from "framer-motion";
import { ExternalLink } from "lucide-react";
import type { Message, Checkpoint, FollowUp } from "@/types";
import BlockRenderer from "./blocks/BlockRenderer";
import CheckpointTrail from "./CheckpointTrail";
import MarkdownBlock from "./blocks/MarkdownBlock";
import Logo from "./Logo";

interface Props {
  message: Message;
  streaming?: boolean;
  checkpoints?: Checkpoint[];
  onFollowUp?: (q: FollowUp) => void;
}

export default function MessageBubble({ message, streaming, checkpoints, onFollowUp }: Props) {
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.15 }} className="flex justify-end">
        <div className="max-w-[85%] rounded-card bg-sunken px-4 py-2.5">
          <p className="text-base whitespace-pre-wrap break-words">{message.content}</p>
        </div>
      </motion.div>
    );
  }

  return (
    <motion.article
      initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.15 }}
      aria-label="Lumen response" aria-busy={streaming || undefined}
    >
      <div className="flex items-center gap-2 mb-3">
        <Logo size={20} />
        <span className="text-sm font-medium">Lumen</span>
      </div>

      {checkpoints && checkpoints.length > 0 && (
        <CheckpointTrail items={checkpoints} active={!!streaming} />
      )}

      {message.blocks && message.blocks.length > 0 ? (
        <BlockRenderer blocks={message.blocks} />
      ) : message.content ? (
        <MarkdownBlock data={{ content: message.content }} />
      ) : streaming ? (
        <ThinkingSkeleton />
      ) : null}

      {message.sources && message.sources.length > 0 && (
        <section aria-label="Sources" className="mt-5">
          <h3 className="meta font-medium mb-2">Sources</h3>
          <ul className="flex flex-wrap gap-1.5">
            {message.sources.slice(0, 8).map((url, i) => (
              <li key={i} className="min-w-0">
                <a
                  href={url} target="_blank" rel="noreferrer"
                  className="tag max-w-full hover:bg-sunken hover:text-ink transition-colors"
                >
                  <ExternalLink size={12} aria-hidden="true" className="shrink-0" />
                  <span className="truncate">{hostnameOf(url)}</span>
                  <span className="sr-only"> (opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}

      {message.followUps && message.followUps.length > 0 && onFollowUp && (
        <section aria-label="Suggested follow-ups" className="mt-5">
          <h3 className="meta font-medium mb-2">Follow up</h3>
          <ul className="flex flex-wrap gap-2">
            {message.followUps.map((f, i) => (
              <li key={i}>
                <button onClick={() => onFollowUp(f)} className="btn-secondary h-8 px-3 text-xs">
                  {f.label}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </motion.article>
  );
}

function ThinkingSkeleton() {
  return (
    <div className="card p-4 space-y-2.5 max-w-md" role="status">
      <span className="sr-only">Lumen is working on your question</span>
      {[100, 88, 70].map((w, i) => (
        <div key={i} className="h-3 rounded-control bg-sunken animate-pulse" style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

function hostnameOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return url; }
}
