import { useState } from "react";

import { answer, answerSummary } from "../lib/asks";
import type { AskEvent, Choice, LineStyle } from "../lib/protocol";
import type { AskEntry, ToolEntry, ToolLine } from "../store/session";
import { ActionButton } from "./Buttons";
import { DiffView, diffCounts } from "./DiffView";

function Pulse() {
  return <span className="blink mx-0.5 size-2 shrink-0 rounded-full bg-primary" />;
}

function Glyph({ status, waiting }: { status: ToolEntry["status"]; waiting: boolean }) {
  if (waiting || status === "running") return <Pulse />;
  if (status === "done") return <span className="text-[13px] text-success">✓</span>;
  return <span className="text-[13px] text-destructive">✗</span>;
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

// A tool's result lines, under ⎿ like in the terminal
function Lines({ lines }: { lines: ToolLine[] }) {
  return (
    <div className="font-mono text-[13px] leading-normal">
      {lines.map((line, i) => (
        <div key={i} className="flex">
          <span className="w-[22px] shrink-0 select-none text-dim">{i === 0 ? "⎿" : ""}</span>
          <span className={`min-w-0 whitespace-pre-wrap break-words ${lineClass(line)}`}>
            {line.text}
          </span>
        </div>
      ))}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return <span className="ml-auto shrink-0 pl-2 text-[10px] text-dim">{open ? "▼" : "▶"}</span>;
}

// How a choice reads as a button. An edit's yes and no accept or reject its diff.
function buttonLabel(ask: AskEvent, choice: Choice, onDiff: boolean) {
  if (ask.kind === "permission") {
    if (choice.value === "yes") return onDiff ? "Accept" : "Allow";
    if (choice.value === "no") return onDiff ? "Reject" : "Deny";
    if (choice.value === "bypass") return "Stop asking";
  }
  return choice.label;
}

function buttonVariant(ask: AskEvent, choice: Choice) {
  if (choice.value === ask.default) return "primary";
  if (choice.value === "no") return "secondary";
  if (choice.value === "bypass") return "quiet";
  return "ghost";
}

function outcome(ask: AskEvent, value: string | null | undefined, onDiff: boolean) {
  if (ask.kind === "permission" && value === "always") {
    return { text: "✓ Accepted · loom won't ask again for these", ok: true };
  }
  if (ask.kind === "permission" && value === "bypass") {
    return { text: "✓ Accepted · loom stops asking for this session", ok: true };
  }
  const { text, ok } = answerSummary(ask, value, onDiff);
  const word = text.replace(/^› /, "");
  const capital = word.charAt(0).toUpperCase() + word.slice(1);
  if (ok === true) return { text: `✓ ${capital}`, ok };
  if (ok === false) return { text: `✗ ${capital}`, ok };
  return { text: capital, ok };
}

// A question on the card, like whether to make the edit above it or run the command.
export function CardAsk({ entry, onDiff }: { entry: AskEntry; onDiff: boolean }) {
  const { ask } = entry;
  if (entry.answer !== undefined) {
    // A denied command's result already says so
    if (ask.kind === "permission" && entry.answer === "no" && !onDiff) return null;
    const { text, ok } = outcome(ask, entry.answer, onDiff);
    const color = ok === true ? "text-success" : ok === false ? "text-del" : "text-dim";
    return (
      <div className={`border-t border-selected px-3.5 py-2 text-[12.5px] ${color}`}>{text}</div>
    );
  }
  return (
    <div className="border-t border-selected bg-background px-3.5 py-3">
      {ask.subject && (
        <pre className="mb-2.5 whitespace-pre-wrap break-words rounded-lg border border-border bg-card px-3 py-2 font-mono text-[12.5px] text-foreground">
          {ask.subject}
        </pre>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <span className="mr-1.5 text-[14px]">{ask.question}</span>
        {ask.choices.map((choice) => (
          <ActionButton
            key={choice.value}
            variant={buttonVariant(ask, choice)}
            hint={choice.value[0]}
            onClick={() => answer(ask, choice.value)}
          >
            {buttonLabel(ask, choice, onDiff)}
          </ActionButton>
        ))}
      </div>
    </div>
  );
}

export function ToolCard({ entry, asks }: { entry: ToolEntry; asks: AskEntry[] }) {
  const onDiff = entry.diffs.length > 0;
  // Diffs show unless folded; the arguments and output of other calls on request. The
  // diffs arrive after the card, so until it's clicked it follows whether there are any.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? onDiff;
  const { added, removed } = diffCounts(entry.diffs);
  const waiting = asks.some((ask) => ask.answer === undefined);

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <button
        onClick={() => setToggled(!open)}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-raised"
      >
        <Glyph status={entry.status} waiting={waiting} />
        <span className="shrink-0 text-[13.5px] font-semibold">{entry.name}</span>
        {entry.detail && (
          <span className="min-w-0 truncate font-mono text-[13px] text-muted-foreground">
            {entry.detail}
          </span>
        )}
        {onDiff && (
          <span className="shrink-0 font-mono text-[12px]">
            <span className="text-add">+{added}</span> <span className="text-del">-{removed}</span>
          </span>
        )}
        <Chevron open={open} />
      </button>

      {entry.lines.length > 0 && (
        <div className="px-3.5 pb-2.5 pl-[38px]">
          <Lines lines={entry.lines} />
        </div>
      )}

      {open &&
        (onDiff ? (
          entry.diffs.map((diff) => <DiffView key={diff.id} diff={diff} inCard />)
        ) : (
          <div className="border-t border-selected px-3.5 py-2.5 text-[12px]">
            <div className="text-dim">Arguments</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-muted-foreground">
              {entry.args ?? "(none)"}
            </pre>
            <div className="mt-2 text-dim">Output</div>
            <pre className="scroll-thin max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-muted-foreground">
              {entry.status === "running" ? "(running)" : entry.output || "(none)"}
            </pre>
          </div>
        ))}

      {asks.map((ask) => (
        <CardAsk key={ask.id} entry={ask} onDiff={onDiff} />
      ))}
    </div>
  );
}

const SUMMARY: Record<string, [string, string, string]> = {
  Read: ["Read", "file", "files"],
  Grep: ["searched", "pattern", "patterns"],
  Glob: ["searched", "pattern", "patterns"],
  List: ["listed", "directory", "directories"],
  Recall: ["recalled", "memory", "memories"],
};

// "Read 2 files, searched 3 patterns"
export function exploredSummary(tools: ToolEntry[]) {
  const counts = new Map<string, { verb: string; one: string; many: string; n: number }>();
  for (const tool of tools) {
    const [verb, one, many] = SUMMARY[tool.name] ?? [tool.name, "call", "calls"];
    const key = `${verb} ${many}`;
    const count = counts.get(key) ?? { verb, one, many, n: 0 };
    count.n++;
    counts.set(key, count);
  }
  const text = [...counts.values()]
    .map(({ verb, one, many, n }) => `${verb.toLowerCase()} ${n} ${n === 1 ? one : many}`)
    .join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// Finished calls that only looked at the project, folded into one card
export function ToolGroup({ tools }: { tools: ToolEntry[] }) {
  const [open, setOpen] = useState(false);
  const failed = tools.some((tool) => tool.status === "failed");
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-raised"
      >
        <Glyph status={failed ? "failed" : "done"} waiting={false} />
        <span className="shrink-0 text-[13.5px] font-semibold">Explored</span>
        <span className="min-w-0 truncate text-[13px] text-muted-foreground">
          {exploredSummary(tools)}
        </span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-selected py-2.5 pl-[38px] pr-3.5 font-mono text-[13px] leading-normal">
          {tools.map((tool) => (
            <div key={tool.id}>
              <div className="min-w-0 break-words">
                <span className="font-bold">{tool.name}</span>{" "}
                {tool.detail && <span className="text-muted-foreground">({tool.detail})</span>}
              </div>
              {tool.lines.length > 0 && <Lines lines={tool.lines} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
