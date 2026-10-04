import { useEffect, useLayoutEffect, useMemo, useRef } from "react";

import {
  type AskEntry,
  type Entry,
  type ToolEntry,
  pendingAsk,
  useSession,
} from "../store/session";
import { Message } from "./Messages";
import { ToolGroup } from "./ToolCard";

// Within this many pixels of the bottom, new output keeps the chat scrolled to the end
const STICK_DISTANCE = 80;

const NO_ASKS: AskEntry[] = [];

// Tools that only look at the project. Finished calls of these in a row show as one
// "Explored" card, like Claude Code's.
const EXPLORING = new Set(["Read", "List", "Glob", "Grep", "Recall"]);

type Item =
  | { kind: "entry"; entry: Entry }
  | { kind: "group"; id: string; tools: ToolEntry[] }
  // Parallel builders' work: each builder's entries, side by side
  | { kind: "lanes"; id: string; lanes: [string, Item[]][] };

// Consecutive entries from parallel builders show as one lane per builder
function laneItems(entries: Entry[], cards: Map<string, AskEntry[]>): Item[] {
  const items: Item[] = [];
  let lanes: Map<string, Entry[]> | null = null;
  let first = "";
  const flush = () => {
    if (lanes) {
      const grouped: [string, Item[]][] = [...lanes].map(([worker, own]) => [
        worker,
        groupTools(own, cards),
      ]);
      items.push({ kind: "lanes", id: `lanes-${first}`, lanes: grouped });
    }
    lanes = null;
  };
  let plain: Entry[] = [];
  for (const entry of entries) {
    if (entry.worker) {
      items.push(...groupTools(plain, cards));
      plain = [];
      if (!lanes) {
        lanes = new Map();
        first = entry.id;
      }
      lanes.set(entry.worker, [...(lanes.get(entry.worker) ?? []), entry]);
    } else {
      flush();
      plain.push(entry);
    }
  }
  items.push(...groupTools(plain, cards));
  flush();
  return items;
}

function groupTools(entries: Entry[], cards: Map<string, AskEntry[]>): Item[] {
  const items: Item[] = [];
  let run: ToolEntry[] = [];
  const flush = () => {
    if (run.length > 1) items.push({ kind: "group", id: `group-${run[0].id}`, tools: run });
    else if (run.length === 1) items.push({ kind: "entry", entry: run[0] });
    run = [];
  };
  for (const entry of entries) {
    const quiet =
      entry.kind === "tool" &&
      EXPLORING.has(entry.name) &&
      entry.status !== "running" &&
      !entry.diffs.length &&
      !cards.has(entry.id);
    if (quiet) {
      run.push(entry);
      continue;
    }
    flush();
    items.push({ kind: "entry", entry });
  }
  flush();
  return items;
}

// A new conversation: the question, above the input. Warnings and errors loom printed
// while starting show above it; its banner doesn't.
export function Welcome() {
  const cwd = useSession((state) => state.session?.cwd ?? "");
  const entries = useSession((state) => state.entries);
  const project = cwd.split(/[\\/]/).filter(Boolean).pop();
  const problems = entries.filter((entry) => entry.kind === "system" && entry.level !== "info");
  return (
    <div className="scroll-thin flex flex-1 flex-col justify-end overflow-y-auto px-6 pb-7">
      <div className="mx-auto flex w-full max-w-[760px] flex-col items-center gap-2.5 text-center">
        {problems.length > 0 && (
          <div className="mb-6 flex w-full flex-col gap-2 text-left">
            {problems.map((entry) => (
              <Message key={entry.id} entry={entry} asks={NO_ASKS} />
            ))}
          </div>
        )}
        <span className="font-mono text-[15px] font-bold text-primary">loom</span>
        <h1 className="m-0 text-balance text-[30px] font-medium tracking-[-0.015em]">
          {project ? `What should we work on in ${project}?` : "What should we work on?"}
        </h1>
      </div>
    </div>
  );
}

const itemKey = (item: Item) => (item.kind === "entry" ? item.entry.id : item.id);

function ChatItem({ item, onCards }: { item: Item; onCards: Map<string, AskEntry[]> }) {
  if (item.kind === "group") return <ToolGroup tools={item.tools} />;
  if (item.kind === "lanes") return <WorkerLanes lanes={item.lanes} onCards={onCards} />;
  return <Message entry={item.entry} asks={onCards.get(item.entry.id) ?? NO_ASKS} />;
}

// The parallel builders' work, a lane each, like a work package's column on a board
function WorkerLanes({
  lanes,
  onCards,
}: {
  lanes: [string, Item[]][];
  onCards: Map<string, AskEntry[]>;
}) {
  const columns = Math.min(lanes.length, 3);
  return (
    <div
      className="grid gap-3"
      style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
      aria-label="Parallel builders"
    >
      {lanes.map(([worker, items]) => {
        const running = items.some(
          (item) =>
            (item.kind === "entry" &&
              item.entry.kind === "tool" &&
              item.entry.status === "running") ||
            (item.kind === "entry" && item.entry.kind === "loom" && item.entry.streaming),
        );
        return (
          <section
            key={worker}
            aria-label={`Builder ${worker}`}
            className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-card/40 p-3 text-[13.5px]"
          >
            <header className="flex items-center gap-2 border-b border-raised pb-2">
              <span
                className={`size-2 shrink-0 rounded-full ${running ? "blink bg-primary" : "bg-success"}`}
              />
              <span className="truncate font-mono text-[12.5px] font-semibold">{worker}</span>
              <span className="ml-auto shrink-0 text-[11px] text-dim">builder</span>
            </header>
            {items.map((item) => (
              <ChatItem key={itemKey(item)} item={item} onCards={onCards} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

export function Chat() {
  const entries = useSession((state) => state.entries);
  const busy = useSession((state) => state.session?.busy ?? false);
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  // Where the chat last scrolled itself to, so its own scrolling isn't mistaken for the
  // user's: the event can arrive after more content, when it no longer looks like the end
  const pinnedAt = useRef(-1);

  function pin() {
    const el = scroller.current;
    if (!el || !stuck.current) return;
    el.scrollTop = el.scrollHeight;
    pinnedAt.current = el.scrollTop;
  }

  useLayoutEffect(pin, [entries, busy]);

  // Content also grows without new messages, like a checkpoint's document arriving or the
  // side pane narrowing the chat: stay at the end then too
  useEffect(() => {
    const el = scroller.current;
    if (!el || !content.current) return;
    const observer = new ResizeObserver(pin);
    observer.observe(el);
    observer.observe(content.current);
    return () => observer.disconnect();
  }, []);

  // Questions about a tool call show on its card, except a plan, which has a card of its own
  const { onCards, items } = useMemo(() => {
    const cards = new Set<string>();
    const onCards = new Map<string, AskEntry[]>();
    const onCard = (entry: Entry) =>
      entry.kind === "ask" &&
      entry.ask.kind !== "plan" &&
      !!entry.ask.tool_id &&
      cards.has(`tool-${entry.ask.tool_id}`);
    for (const entry of entries) {
      if (entry.kind === "tool") cards.add(entry.id);
      if (entry.kind === "ask" && onCard(entry)) {
        const card = `tool-${entry.ask.tool_id}`;
        onCards.set(card, [...(onCards.get(card) ?? []), entry]);
      }
    }
    const shown = entries.filter((entry) => !onCard(entry));
    return { onCards, items: laneItems(shown, onCards) };
  }, [entries]);

  const last = entries[entries.length - 1];
  const active =
    (last?.kind === "loom" && last.streaming) ||
    (last?.kind === "tool" && last.status === "running");
  const working = busy && !active && !pendingAsk(entries);

  return (
    <div
      ref={scroller}
      onScroll={(event) => {
        const el = event.currentTarget;
        const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_DISTANCE;
        if (!atEnd && el.scrollTop === pinnedAt.current) return;
        stuck.current = atEnd;
      }}
      className="scroll-thin min-h-0 flex-1 overflow-y-auto"
    >
      <div ref={content} className="mx-auto flex max-w-[760px] flex-col gap-[22px] px-6 pb-7 pt-9">
        {items.map((item) => (
          <ChatItem key={itemKey(item)} item={item} onCards={onCards} />
        ))}
        {working && (
          <div className="flex items-center gap-2 text-[13.5px] text-muted-foreground">
            <span className="blink text-primary">●</span>
            Working… <span className="text-dim">esc to interrupt</span>
          </div>
        )}
      </div>
    </div>
  );
}
