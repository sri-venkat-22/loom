import { create } from "zustand";

import type { AskEvent, Level, ServerEvent, SessionEvent } from "../lib/protocol";

export type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "loom"; id: string; text: string; reasoning: string; streaming: boolean }
  | { kind: "system"; id: string; level: Level; text: string }
  // answer is undefined while loom waits, and null if the question was dropped
  | { kind: "ask"; id: string; ask: AskEvent; answer?: string | null };

export type Connection = "connecting" | "open" | "closed";

interface SessionState {
  connection: Connection;
  session: SessionEvent | null;
  entries: Entry[];
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

    case "ask":
      return [...entries, { kind: "ask", id: `ask-${event.ask_id}`, ask: event }];

    case "ask_resolved":
      return update(entries, `ask-${event.ask_id}`, (entry) =>
        entry.kind === "ask" ? { ...entry, answer: event.value } : entry,
      );

    case "turn_end":
      // A reply cut off by Esc never got its end
      return entries.map((entry) =>
        entry.kind === "loom" && entry.streaming ? { ...entry, streaming: false } : entry,
      );

    default:
      return entries;
  }
}

export const useSession = create<SessionState>((set) => ({
  connection: "connecting",
  session: null,
  entries: [],
  setConnection: (connection) => set({ connection }),
  // The server replays the whole conversation to every new connection
  reset: () => set({ session: null, entries: [] }),
  apply: (event) =>
    set((state) =>
      event.type === "session" ? { session: event } : { entries: reduce(state.entries, event) },
    ),
}));

export const pendingAsk = (entries: Entry[]) => {
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.kind === "ask" && entry.answer === undefined) return entry;
  }
  return undefined;
};
