import { createContext, useContext, useState } from "react";

import type { TaskEvent } from "../lib/protocol";
import { send } from "../lib/socket";
import { type AskEntry, type Entry, type ToolEntry, useSession } from "../store/session";
import { DiffView } from "./DiffView";
import { Markdown } from "./Markdown";
import { CardAsk, ToolCard } from "./ToolCard";

// What a Task card shows: each Task card's own entries (the sub-agent's tool cards and
// messages), and the questions on those cards
export const TaskEntries = createContext<{
  children: Map<string, Entry[]>;
  onCards: Map<string, AskEntry[]>;
}>({ children: new Map(), onCards: new Map() });

const NO_ENTRIES: Entry[] = [];
const NO_ASKS: AskEntry[] = [];

// How many of a running task's latest entries show while it's folded
const RECENT = 3;

const FINISHED_OK = new Set(["done", "incomplete"]);

export const isRunning = (task: TaskEvent | undefined, entry: ToolEntry) =>
  task ? task.status === "pending" || task.status === "running" : entry.status === "running";

// The report without its "[task: ...]" footer, which the card's summary says
const reportOf = (output: string) => output.replace(/\n*\[task: [^\]\n]*\]\s*$/, "");

function TaskGlyph({ running, ok }: { running: boolean; ok: boolean }) {
  if (running) return <span className="blink mx-0.5 size-2 shrink-0 rounded-full bg-primary" />;
  if (ok) return <span className="text-[13px] text-success">✓</span>;
  return <span className="text-[13px] text-destructive">✗</span>;
}

function ChildEntry({ entry, onCards }: { entry: Entry; onCards: Map<string, AskEntry[]> }) {
  if (entry.kind === "tool")
    return <ToolCard entry={entry} asks={onCards.get(entry.id) ?? NO_ASKS} />;
  if (entry.kind === "diff") return <DiffView diff={entry.diff} />;
  if (entry.kind === "system") {
    const color =
      entry.level === "error"
        ? "text-destructive"
        : entry.level === "warning"
          ? "text-warning"
          : "text-muted-foreground";
    return (
      <pre className={`m-0 whitespace-pre-wrap break-words font-mono text-[12.5px] ${color}`}>
        {entry.text}
      </pre>
    );
  }
  return null;
}

// A sub-agent's task: its latest tool cards live while it runs, the rest a click away
export function TaskCard({ entry, asks }: { entry: ToolEntry; asks: AskEntry[] }) {
  const task = useSession((state) => state.tasks[entry.id]);
  const { children, onCards } = useContext(TaskEntries);
  const own = children.get(entry.id) ?? NO_ENTRIES;
  const [open, setOpen] = useState(false);
  const running = isRunning(task, entry);
  const ok = task ? FINISHED_OK.has(task.status) : entry.status !== "failed";

  // Folded: while it runs, the latest few and any card waiting for an answer; once it's
  // done, none
  const waiting = (child: Entry) =>
    (onCards.get(child.id) ?? []).some((ask) => ask.answer === undefined);
  const shown = open
    ? own
    : own.filter((child, index) => waiting(child) || (running && index >= own.length - RECENT));
  const tools = own.filter((child) => child.kind === "tool").length;
  const hidden = tools - shown.filter((child) => child.kind === "tool").length;
  const title = `Task${task ? ` ${task.number}` : ""}: ${entry.detail}`;
  const meta = [
    task?.agent,
    running && tools ? `${tools} tool ${tools === 1 ? "use" : "uses"}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section aria-label={title} className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="flex items-start gap-2.5 px-3.5 py-2.5">
        <span className="flex h-[21px] items-center">
          <TaskGlyph running={running} ok={ok} />
        </span>
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          title={open ? "Fold the transcript" : "Show the whole transcript"}
          className="min-w-0 flex-1 text-left"
        >
          <span className="flex items-center gap-2">
            <span className="shrink-0 text-[13.5px] font-semibold">Task</span>
            <span className="min-w-0 truncate text-[13.5px]">{entry.detail}</span>
            <span className="ml-auto shrink-0 pl-1 text-[10px] text-dim">{open ? "▼" : "▶"}</span>
          </span>
          {meta && <span className="block font-mono text-[11.5px] text-dim">{meta}</span>}
        </button>
        {running && task && (
          <button
            onClick={() => send({ type: "stop_task", agent_id: task.agent_id })}
            title="Stop this task; the agent carries on without it"
            className="shrink-0 rounded-md px-2 py-0.5 text-[12px] text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            Stop
          </button>
        )}
      </div>

      {open && task?.prompt && (
        <div className="border-t border-selected px-3.5 py-2.5 text-[12px]">
          <div className="text-dim">Prompt</div>
          <pre className="whitespace-pre-wrap break-words font-mono text-muted-foreground">
            {task.prompt}
          </pre>
        </div>
      )}

      {(shown.length > 0 || (running && !own.length)) && (
        <div className="flex flex-col gap-2 border-t border-selected py-2.5 pl-[38px] pr-3.5">
          {!open && running && hidden > 0 && (
            <button
              onClick={() => setOpen(true)}
              className="self-start text-[12.5px] text-dim hover:text-soft"
            >
              … +{hidden} more tool {hidden === 1 ? "use" : "uses"}
            </button>
          )}
          {shown.map((child) => (
            <ChildEntry key={child.id} entry={child} onCards={onCards} />
          ))}
          {running && !own.length && (
            <div className="text-[12.5px] text-dim">
              {task?.status === "pending" ? "Waiting to start…" : "Starting…"}
            </div>
          )}
        </div>
      )}

      {!running && (
        <div
          className={`border-t border-selected px-3.5 py-2 text-[12.5px] ${ok ? "text-dim" : "text-destructive"}`}
        >
          {task?.summary || (entry.status === "failed" ? "Failed" : "Done")}
        </div>
      )}

      {open && !running && entry.output && (
        <div className="border-t border-selected px-3.5 py-2.5 text-[14px]">
          <div className="mb-1 text-[12px] text-dim">Report</div>
          <Markdown text={reportOf(entry.output)} />
        </div>
      )}

      {asks.map((ask) => (
        <CardAsk key={ask.id} entry={ask} onDiff={false} />
      ))}
    </section>
  );
}

// Tasks the agent started in one reply, which run at once: side by side
export function TaskBatch({
  entries,
  asks,
}: {
  entries: ToolEntry[];
  asks: Map<string, AskEntry[]>;
}) {
  const columns = Math.min(entries.length, 2);
  return (
    <div
      className="grid grid-cols-1 gap-3 md:[grid-template-columns:var(--task-columns)]"
      style={{ ["--task-columns" as string]: `repeat(${columns}, minmax(0, 1fr))` }}
      aria-label="Parallel tasks"
    >
      {entries.map((entry) => (
        <TaskCard key={entry.id} entry={entry} asks={asks.get(entry.id) ?? NO_ASKS} />
      ))}
    </div>
  );
}
