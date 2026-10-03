import { useMemo } from "react";

import { type Token, useHighlighted } from "../lib/highlight";
import type { DiffEvent, DiffLine } from "../lib/protocol";

const HAS_CODE = new Set<DiffLine["kind"]>(["add", "del", "ctx"]);

export function diffCounts(diffs: DiffEvent[]) {
  let added = 0;
  let removed = 0;
  for (const diff of diffs) {
    for (const line of diff.lines) {
      if (line.kind === "add") added++;
      if (line.kind === "del") removed++;
    }
  }
  return { added, removed };
}

function Code({ text, tokens }: { text: string; tokens?: Token[] }) {
  if (!tokens) return <>{text}</>;
  return (
    <>
      {tokens.map((token, i) => (
        <span key={i} className="shiki-token" style={token.style}>
          {token.text}
        </span>
      ))}
    </>
  );
}

// A unified diff with old and new line numbers, +/- gutters and syntax highlighting. In a
// card it fills the card's width under a rule; elsewhere it has its own border.
export function DiffView({ diff, inCard = false }: { diff: DiffEvent; inCard?: boolean }) {
  const code = useMemo(
    () => diff.lines.filter((line) => HAS_CODE.has(line.kind)).map((line) => line.text),
    [diff],
  );
  const highlighted = useHighlighted(code, diff.file);

  let codeLine = 0;
  return (
    <pre
      className={`scroll-thin m-0 overflow-x-auto py-1 font-mono text-[12.5px] leading-5 ${
        inCard ? "border-t border-selected" : "rounded-[10px] border border-border bg-card"
      }`}
    >
      {diff.lines.map((line, i) => {
        if (line.kind === "gap") {
          return (
            <div key={i} className="select-none bg-background/40 pl-[4.5rem] text-dim">
              ⋮{line.text && <span className="pl-3">{line.text}</span>}
            </div>
          );
        }
        if (line.kind === "note") {
          return (
            <div key={i} className="pl-[5.5rem] text-dim">
              {line.text}
            </div>
          );
        }
        const tokens = highlighted?.[codeLine++];
        const background =
          line.kind === "add" ? "bg-add-bg" : line.kind === "del" ? "bg-del-bg" : "";
        const marker = line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
        const markerColor =
          line.kind === "add" ? "text-add" : line.kind === "del" ? "text-del" : "text-dim";
        return (
          <div key={i} className={`flex min-w-max ${background}`}>
            <span className="w-[34px] shrink-0 select-none pr-2 text-right text-faint">
              {line.old ?? ""}
            </span>
            <span className="w-[34px] shrink-0 select-none pr-2 text-right text-faint">
              {line.new ?? ""}
            </span>
            <span className={`w-4 shrink-0 select-none ${markerColor}`}>{marker}</span>
            <span className="whitespace-pre pr-3.5">
              <Code text={line.text} tokens={tokens} />
            </span>
          </div>
        );
      })}
    </pre>
  );
}
