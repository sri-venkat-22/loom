import { useEffect, useMemo, useRef, useState } from "react";

import { answerWithKey } from "../lib/asks";
import type { Command } from "../lib/protocol";
import { send } from "../lib/socket";
import { useSession } from "../store/session";
import { SlashMenu, matchingCommands } from "./SlashMenu";

const MAX_HEIGHT = 240;
const NO_COMMANDS: Command[] = [];

export function Composer() {
  const [text, setText] = useState("");
  const [selected, setSelected] = useState(0);
  // Esc hides the menu until the text changes
  const [dismissed, setDismissed] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const open = useSession((state) => state.connection === "open");
  const commands = useSession((state) => state.session?.commands ?? NO_COMMANDS);

  const matches = useMemo(
    () => (dismissed ? [] : matchingCommands(text, commands)),
    [text, commands, dismissed],
  );

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

  function change(value: string) {
    setText(value);
    setSelected(0);
    setDismissed(false);
  }

  function run(message: string) {
    message = message.trim();
    if (!message || busy || !open) return;
    if (send({ type: "input", text: message })) change("");
  }

  function pick(command: Command) {
    run(command.cmd);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    const plain = !event.metaKey && !event.ctrlKey && !event.altKey;
    // With an empty input, a choice's first letter answers loom's question: y accepts
    // an edit and n rejects it
    if (!text && plain && answerWithKey(event.key)) {
      event.preventDefault();
      return;
    }

    if (matches.length) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        setSelected((i) => (i + step + matches.length) % matches.length);
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        change(matches[selected].cmd + " ");
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        pick(matches[selected]);
        return;
      }
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        setDismissed(true);
        return;
      }
    }

    if (event.key === "Enter" && (event.metaKey || event.ctrlKey || !event.shiftKey)) {
      event.preventDefault();
      run(text);
    }
  }

  return (
    <div className="mx-auto w-full max-w-[868px] px-5">
      <div className="relative border-b border-border pb-3">
        {matches.length > 0 && (
          <SlashMenu
            commands={matches}
            selected={Math.min(selected, matches.length - 1)}
            onHover={setSelected}
            onPick={pick}
          />
        )}
        <div className="flex items-end gap-3">
          <textarea
            ref={input}
            rows={1}
            autoFocus
            value={text}
            onChange={(event) => change(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask loom to…"
            className="flex-1 resize-none bg-transparent text-[16px] leading-6 outline-none placeholder:text-dim"
          />
          <button
            onClick={() => run(text)}
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
