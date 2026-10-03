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

type Item = { kind: "entry"; entry: Entry } | { kind: "group"; id: string; tools: ToolEntry[] };

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

  // Questions about a tool call show on its card
  const { onCards, items } = useMemo(() => {
    const cards = new Set<string>();
    const onCards = new Map<string, AskEntry[]>();
    for (const entry of entries) {
      if (entry.kind === "tool") cards.add(entry.id);
      if (entry.kind === "ask" && entry.ask.tool_id && cards.has(`tool-${entry.ask.tool_id}`)) {
        const card = `tool-${entry.ask.tool_id}`;
        onCards.set(card, [...(onCards.get(card) ?? []), entry]);
      }
    }
    const shown = entries.filter(
      (entry) =>
        entry.kind !== "ask" || !entry.ask.tool_id || !cards.has(`tool-${entry.ask.tool_id}`),
    );
    return { onCards, items: groupTools(shown, onCards) };
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
        {items.map((item) =>
          item.kind === "group" ? (
            <ToolGroup key={item.id} tools={item.tools} />
          ) : (
            <Message
              key={item.entry.id}
              entry={item.entry}
              asks={onCards.get(item.entry.id) ?? NO_ASKS}
            />
          ),
        )}
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
