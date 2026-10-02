import { useLayoutEffect, useMemo, useRef } from "react";

import { type AskEntry, pendingAsk, useSession } from "../store/session";
import { Message } from "./Messages";

// Within this many pixels of the bottom, new output keeps the chat scrolled to the end
const STICK_DISTANCE = 80;

const NO_ASKS: AskEntry[] = [];

export function Chat() {
  const entries = useSession((state) => state.entries);
  const busy = useSession((state) => state.session?.busy ?? false);
  const scroller = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stuck.current) el.scrollTop = el.scrollHeight;
  }, [entries, busy]);

  // Questions about a tool call show on its card
  const { onCards, cards } = useMemo(() => {
    const cards = new Set<string>();
    const onCards = new Map<string, AskEntry[]>();
    for (const entry of entries) {
      if (entry.kind === "tool") cards.add(entry.id);
      if (entry.kind === "ask" && entry.ask.tool_id && cards.has(`tool-${entry.ask.tool_id}`)) {
        const card = `tool-${entry.ask.tool_id}`;
        onCards.set(card, [...(onCards.get(card) ?? []), entry]);
      }
    }
    return { onCards, cards };
  }, [entries]);
  const shown = entries.filter(
    (entry) =>
      entry.kind !== "ask" || !entry.ask.tool_id || !cards.has(`tool-${entry.ask.tool_id}`),
  );

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
        stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_DISTANCE;
      }}
      className="scroll-thin flex-1 overflow-y-auto"
    >
      <div className="mx-auto max-w-[868px] space-y-5 px-5 pb-6 pt-[58px]">
        {entries.length === 0 && (
          <div>
            <div className="mb-2 text-[13px] text-muted-foreground">loom</div>
            <div className="text-[16px] leading-6">
              Ready when you are. Ask loom to inspect a project, plan a change, or run a command.
            </div>
          </div>
        )}
        {shown.map((entry) => (
          <Message key={entry.id} entry={entry} asks={onCards.get(entry.id) ?? NO_ASKS} />
        ))}
        {working && (
          <div className="text-dim">
            <span className="blink text-primary">●</span> working…{" "}
            <span className="text-dim">(esc to interrupt)</span>
          </div>
        )}
      </div>
    </div>
  );
}
