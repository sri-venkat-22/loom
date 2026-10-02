import { Fragment, useState } from "react";

import { answer, answerSummary, choiceLabel } from "../lib/asks";
import type { LineStyle } from "../lib/protocol";
import type { AskEntry, ToolEntry, ToolLine } from "../store/session";
import { DiffView, diffCounts } from "./DiffView";

function Glyph({ status }: { status: ToolEntry["status"] }) {
  if (status === "running") return <span className="blink text-primary">●</span>;
  if (status === "done") return <span className="text-success">✓</span>;
  return <span className="text-destructive">✗</span>;
}

const STYLE_CLASS: Record<NonNullable<LineStyle>, string> = {
  done: "text-dim line-through",
  warning: "text-warning",
  error: "text-destructive",
  bold: "font-bold text-foreground",
  dim: "text-dim",
};

function lineClass(line: ToolLine) {
  if (line.style) return STYLE_CLASS[line.style];
  return line.error ? "text-destructive" : "text-muted-foreground";
}

// A question on the card, like whether to make the edit above it or run the command.
export function CardAsk({ entry, onDiff }: { entry: AskEntry; onDiff: boolean }) {
  const { ask } = entry;
  if (entry.answer !== undefined) {
    // A denied call's result already says so
    if (ask.kind === "permission" && entry.answer === "no") return null;
    const { text, ok } = answerSummary(ask, entry.answer, onDiff);
    const color = ok === true ? "text-success" : ok === false ? "text-destructive" : "text-dim";
    return <div className={`mt-1 text-[12px] ${color}`}>{text}</div>;
  }
  return (
    <div className="mt-2">
      {!onDiff && <div className="text-foreground">{ask.question}</div>}
      {ask.subject && (
        <pre className="mt-1 whitespace-pre-wrap break-words border-l border-border pl-3 font-mono text-[13px] text-foreground">
          {ask.subject}
        </pre>
      )}
      <div className="mt-1 text-[12px] text-dim">
        {onDiff && <span className="text-muted-foreground">{ask.question} </span>}
        {ask.choices.map((choice, i) => (
          <Fragment key={choice.value}>
            {i > 0 && " · "}
            <button onClick={() => answer(ask, choice.value)} className="hover:text-foreground">
              <span className="text-muted-foreground">{choice.value[0]}</span>{" "}
              {choiceLabel(ask, choice, onDiff)}
            </button>
          </Fragment>
        ))}
      </div>
    </div>
  );
}

export function ToolCard({ entry, asks }: { entry: ToolEntry; asks: AskEntry[] }) {
  const [open, setOpen] = useState(false);
  const { added, removed } = diffCounts(entry.diffs);
  const onDiff = entry.diffs.length > 0;

  return (
    <div className="text-[14px]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-baseline gap-2 text-left"
      >
        <Glyph status={entry.status} />
        <span className="shrink-0 font-bold">{entry.name}</span>
        {entry.detail && (
          <span className="min-w-0 truncate text-muted-foreground">({entry.detail})</span>
        )}
        {onDiff && (
          <span className="shrink-0 text-[12px]">
            <span className="text-add">+{added}</span> <span className="text-del">-{removed}</span>
          </span>
        )}
        <span className="ml-auto shrink-0 text-[12px] text-dim">{open ? "▾" : "▸"}</span>
      </button>

      <div className="ml-4">
        {entry.diffs.map((diff) => (
          <DiffView key={diff.id} diff={diff} />
        ))}
        {asks.map((ask) => (
          <CardAsk key={ask.id} entry={ask} onDiff={onDiff} />
        ))}
        {entry.lines.length > 0 && (
          <div className="mt-0.5 font-mono text-[13px]">
            {entry.lines.map((line, i) => (
              <div key={i} className="flex">
                <span className="w-6 shrink-0 select-none text-dim">{i === 0 ? "⎿" : ""}</span>
                <span className={`min-w-0 whitespace-pre-wrap break-words ${lineClass(line)}`}>
                  {line.text}
                </span>
              </div>
            ))}
          </div>
        )}
        {open && (
          <div className="mt-1 border-l border-border pl-3 text-[12px]">
            <div className="text-dim">args</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-muted-foreground">
              {entry.args ?? "(none)"}
            </pre>
            <div className="mt-2 text-dim">output</div>
            <pre className="scroll-thin max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-muted-foreground">
              {entry.status === "running" ? "(running)" : entry.output || "(none)"}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
