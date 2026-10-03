import { create } from "zustand";

import type {
  AskEvent,
  DiffEvent,
  Level,
  LineStyle,
  ServerEvent,
  SessionEvent,
} from "../lib/protocol";

export interface ToolLine {
  text: string;
  error: boolean;
  style: LineStyle;
}

export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "loom"; id: string; text: string; reasoning: string; streaming: boolean }
  | { kind: "system"; id: string; level: Level; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      detail: string;
      args: string | null;
      status: "running" | "done" | "failed";
      lines: ToolLine[];
      output: string;
      diffs: DiffEvent[];
    }
  // A diff that isn't on a tool's card
  | { kind: "diff"; id: string; diff: DiffEvent }
  // answer is undefined while loom waits, and null if the question was dropped
  | { kind: "ask"; id: string; ask: AskEvent; answer?: string | null };

export type ToolEntry = Extract<Entry, { kind: "tool" }>;
export type AskEntry = Extract<Entry, { kind: "ask" }>;

export type Connection = "connecting" | "open" | "closed";

// The terminal keeps this much of the end of the commands' output
const MAX_TERMINAL = 200_000;

interface SessionState {
  connection: Connection;
  session: SessionEvent | null;
  entries: Entry[];
  // The output of commands like /run, for the side pane's terminal
  terminal: string;
  setConnection: (connection: Connection) => void;
  reset: () => void;
  apply: (event: ServerEvent) => void;
}

let nextId = 0;
const newId = (prefix: string) => `${prefix}${++nextId}`;

function update(entries: Entry[], id: string, change: (entry: Entry) => Entry): Entry[] {
  const index = entries.findIndex((entry) => entry.id === id);
  if (index < 0) return entries;
  const copy = entries.slice();
  copy[index] = change(entries[index]);
  return copy;
}

function reduce(entries: Entry[], event: ServerEvent): Entry[] {
  switch (event.type) {
    case "user":
      return [...entries, { kind: "user", id: newId("u"), text: event.text }];

    case "assistant_delta": {
      const id = `loom-${event.id}`;
      if (!entries.some((entry) => entry.id === id)) {
        const { text, reasoning } = event;
        return [...entries, { kind: "loom", id, text, reasoning, streaming: true }];
      }
      return update(entries, id, (entry) =>
        entry.kind !== "loom"
          ? entry
          : event.replace
            ? { ...entry, text: event.text, reasoning: event.reasoning }
            : {
                ...entry,
                text: entry.text + event.text,
                reasoning: entry.reasoning + event.reasoning,
              },
      );
    }

    case "assistant_end":
      return update(entries, `loom-${event.id}`, (entry) =>
        entry.kind === "loom" ? { ...entry, streaming: false } : entry,
      );

    case "system": {
      // Consecutive lines of the same kind read as one block
      const last = entries[entries.length - 1];
      if (last?.kind === "system" && last.level === event.level) {
        return [...entries.slice(0, -1), { ...last, text: `${last.text}\n${event.text}` }];
      }
      return [...entries, { kind: "system", id: newId("s"), level: event.level, text: event.text }];
    }

    case "tool_start":
      return [
        ...entries,
        {
          kind: "tool",
          id: `tool-${event.id}`,
          name: event.name,
          detail: event.detail,
          args: event.args,
          status: "running",
          lines: [],
          output: "",
          diffs: [],
        },
      ];

    case "tool_output":
      return update(entries, `tool-${event.id}`, (entry) =>
        entry.kind === "tool"
          ? {
              ...entry,
              lines: [
                ...entry.lines,
                ...event.lines.map((text, i) => ({
                  text,
                  error: event.error,
                  style: event.styles[i] ?? null,
                })),
              ],
            }
          : entry,
      );

    case "tool_end":
      return update(entries, `tool-${event.id}`, (entry) =>
        entry.kind === "tool" ? { ...entry, status: event.status, output: event.output } : entry,
      );

    case "diff": {
      const card = `tool-${event.tool_id}`;
      if (event.tool_id && entries.some((entry) => entry.id === card)) {
        return update(entries, card, (entry) =>
          entry.kind === "tool" ? { ...entry, diffs: [...entry.diffs, event] } : entry,
        );
      }
      return [...entries, { kind: "diff", id: `diff-${event.id}`, diff: event }];
    }

    case "ask":
      return [...entries, { kind: "ask", id: `ask-${event.ask_id}`, ask: event }];

    case "ask_resolved":
      return update(entries, `ask-${event.ask_id}`, (entry) =>
        entry.kind === "ask" ? { ...entry, answer: event.value } : entry,
      );

    case "turn_end":
      // A reply or tool call cut off by Esc never got its end
      return entries.map((entry) =>
        entry.kind === "loom" && entry.streaming
          ? { ...entry, streaming: false }
          : entry.kind === "tool" && entry.status === "running"
            ? { ...entry, status: event.status === "cancelled" ? "failed" : "done" }
            : entry,
      );

    default:
      return entries;
  }
}

export const useSession = create<SessionState>((set) => ({
  connection: "connecting",
  session: null,
  entries: [],
  terminal: "",
  setConnection: (connection) => set({ connection }),
  // The server replays the whole conversation to every new connection
  reset: () => set({ session: null, entries: [], terminal: "" }),
  apply: (event) =>
    set((state) => {
      if (event.type === "session") return { session: event };
      if (event.type === "conversation") return { entries: [] };
      if (event.type === "terminal") {
        const gap = event.start && state.terminal ? "\n" : "";
        return { terminal: (state.terminal + gap + event.text).slice(-MAX_TERMINAL) };
      }
      return { entries: reduce(state.entries, event) };
    }),
}));

export const pendingAsk = (entries: Entry[]): AskEntry | undefined => {
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.kind === "ask" && entry.answer === undefined) return entry;
  }
  return undefined;
};

// Whether nothing has happened in the conversation yet but loom's own messages, like the
// banner it prints at startup
export const isFresh = (entries: Entry[]): boolean =>
  entries.every((entry) => entry.kind === "system");
