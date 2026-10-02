import { useEffect, useRef, useState } from "react";

import { send } from "../lib/socket";
import { pendingAsk, useSession } from "../store/session";

const MAX_HEIGHT = 240;

export function Composer() {
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const open = useSession((state) => state.connection === "open");

  // Grow with the text, up to MAX_HEIGHT
  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [text]);

  // Typing anywhere goes to the input
  useEffect(() => {
    if (!busy) input.current?.focus();
  }, [busy]);

  function submit() {
    const message = text.trim();
    if (!message || busy || !open) return;
    if (send({ type: "input", text: message })) setText("");
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    // With an empty input, a choice's first letter answers loom's question
    const ask = pendingAsk(useSession.getState().entries);
    if (ask && !text && ask.ask.kind !== "prompt" && event.key.length === 1) {
      const key = event.key.toLowerCase();
      const choice = ask.ask.choices.find((c) => c.value.toLowerCase().startsWith(key));
      if (choice && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        send({ type: "answer", ask_id: ask.ask.ask_id, value: choice.value });
        return;
      }
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey || !event.shiftKey)) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="mx-auto w-full max-w-[868px] px-5">
      <div className="relative border-b border-border pb-3">
        <div className="flex items-end gap-3">
          <textarea
            ref={input}
            rows={1}
            autoFocus
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask loom to…"
            className="flex-1 resize-none bg-transparent text-[16px] leading-6 outline-none placeholder:text-dim"
          />
          <button
            onClick={submit}
            aria-label="Send message"
            className="mb-0.5 shrink-0 text-dim transition-colors hover:text-foreground"
          >
            ↵
          </button>
        </div>
      </div>
    </div>
  );
}
