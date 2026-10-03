import { useEffect, useMemo, useRef, useState } from "react";

import { type Alias, api } from "../lib/api";
import { answerWithKey } from "../lib/asks";
import { LOCAL_COMMANDS, runLocal } from "../lib/localCommands";
import type { Command, PermissionMode } from "../lib/protocol";
import { send } from "../lib/socket";
import { isFresh, useSession } from "../store/session";
import { useUi } from "../store/ui";
import { SlashMenu, matchingCommands } from "./SlashMenu";
import { StatusLine, shortModel } from "./StatusLine";

const MAX_HEIGHT = 220;
const NO_COMMANDS: Command[] = [];

const MODES: Record<PermissionMode, { label: string; dot: string }> = {
  ask: { label: "Ask before edits", dot: "bg-muted-foreground" },
  "accept-edits": { label: "Accept edits", dot: "bg-add" },
  plan: { label: "Plan mode", dot: "bg-code" },
  bypass: { label: "Bypass permissions", dot: "bg-destructive" },
};

// The modes Shift-Tab cycles through, as in the terminal; bypass is only chosen on purpose
const CYCLED: PermissionMode[] = ["ask", "accept-edits", "plan"];

function nextMode(mode: PermissionMode): PermissionMode {
  const i = CYCLED.indexOf(mode);
  return CYCLED[(i + 1) % CYCLED.length];
}

const SUGGESTIONS = [
  "Explain this codebase",
  "Find and fix a bug",
  "Run the tests and fix failures",
  "/project new …",
];

// Under the input of a new conversation, things to ask loom
export function Suggestions() {
  const fillInput = useUi((state) => state.fillInput);
  return (
    <div className="flex-1 px-6 pt-1.5">
      <div className="mx-auto flex max-w-[760px] flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((label) => (
          <button
            key={label}
            onClick={() => fillInput(label.endsWith("…") ? label.slice(0, -1) : label)}
            className="rounded-full border border-border-strong px-3.5 py-[7px] text-[13px] text-subtle hover:bg-raised hover:text-foreground"
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

// The main model and its aliases, above the input's model button
function ModelMenu({ onClose }: { onClose: () => void }) {
  const model = useSession((state) => state.session?.model ?? "");
  const weak = useSession((state) => state.session?.weak_model ?? "");
  const idle = useSession((state) => state.connection === "open" && !state.session?.busy);
  const [aliases, setAliases] = useState<Alias[]>([]);
  const [other, setOther] = useState("");

  useEffect(() => {
    api
      .models()
      .then((found) => setAliases(found.aliases))
      .catch(() => setAliases([]));
  }, []);

  function choose(name: string) {
    if (!idle || !name.trim()) return;
    send({ type: "input", text: `/model ${name.trim()}` });
    onClose();
  }

  const known = aliases.some((a) => a.model === model || a.alias === model);

  return (
    <div className="absolute bottom-full right-6 z-10 mb-2 w-80 rounded-[14px] border border-border-strong bg-popover p-1.5 shadow-[0_12px_32px_rgb(0_0_0/0.45)]">
      <div className="px-2.5 pb-1 pt-1.5 text-[12px] text-dim">Main model</div>
      <div className="scroll-thin max-h-64 overflow-y-auto">
        {!known && model && (
          <div className="flex items-center gap-2.5 rounded-lg px-2.5 py-2">
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="text-[13.5px]">{shortModel(model)}</span>
              <span className="truncate font-mono text-[11.5px] text-dim">{model}</span>
            </span>
            <span className="text-[13px] text-primary">✓</span>
          </div>
        )}
        {aliases.map((alias) => {
          const current = alias.model === model || alias.alias === model;
          return (
            <button
              key={alias.alias}
              disabled={!idle}
              onClick={() => choose(alias.alias)}
              className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left enabled:hover:bg-popover-selected"
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="text-[13.5px]">{alias.alias}</span>
                <span className="truncate font-mono text-[11.5px] text-dim">{alias.model}</span>
              </span>
              {current && <span className="text-[13px] text-primary">✓</span>}
            </button>
          );
        })}
      </div>
      <input
        value={other}
        disabled={!idle}
        onChange={(event) => setOther(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") choose(other);
          if (event.key === "Escape") onClose();
        }}
        placeholder="Another model, like openai/gpt-4o"
        className="mx-1 my-1.5 w-[calc(100%-0.5rem)] rounded-lg border border-border-strong bg-background px-2.5 py-1.5 font-mono text-[12px] outline-none placeholder:text-dim"
      />
      {weak && (
        <div className="mt-1 border-t border-border-strong px-2.5 pb-1 pt-2 text-[12px] text-dim">
          Weak model · <span className="font-mono">{weak}</span>
        </div>
      )}
    </div>
  );
}

export function Composer() {
  const [text, setText] = useState("");
  const [selected, setSelected] = useState(0);
  // Esc hides the menu until the text changes
  const [dismissed, setDismissed] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const open = useSession((state) => state.connection === "open");
  const empty = useSession((state) => isFresh(state.entries));
  const model = useSession((state) => state.session?.model ?? "");
  const loomMode = useSession((state) => state.session?.permission_mode ?? null);
  // Modes asked for that loom hasn't confirmed yet, in order, so quick presses keep
  // cycling. loom confirms each in turn; any other change, like answering b to an
  // approval, wins over them.
  const [asked, setAsked] = useState<PermissionMode[]>([]);
  useEffect(() => setAsked((queue) => (queue[0] === loomMode ? queue.slice(1) : [])), [loomMode]);
  const mode = loomMode && (asked[asked.length - 1] ?? loomMode);
  const loomCommands = useSession((state) => state.session?.commands ?? NO_COMMANDS);
  const commands = useMemo(
    () => [...loomCommands, ...LOCAL_COMMANDS].sort((a, b) => a.cmd.localeCompare(b.cmd)),
    [loomCommands],
  );

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

  // ⌘K: every command, in the menu above the input
  const paletteRequests = useUi((state) => state.paletteRequests);
  useEffect(() => {
    if (!paletteRequests) return;
    change("/");
    input.current?.focus();
  }, [paletteRequests]);

  // A suggestion or another part of the page fills the input
  const fill = useUi((state) => state.fill);
  useEffect(() => {
    if (!fill.n) return;
    change(fill.text);
    input.current?.focus();
  }, [fill]);

  // A click outside or Esc closes the model menu
  useEffect(() => {
    if (!modelOpen) return;
    function onDown(event: MouseEvent) {
      if (!box.current?.contains(event.target as Node)) setModelOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setModelOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [modelOpen]);

  function change(value: string) {
    setText(value);
    setSelected(0);
    setDismissed(false);
  }

  function run(message: string) {
    message = message.trim();
    if (!message) return;
    if (runLocal(message)) {
      change("");
      return;
    }
    if (message === "/model") {
      change("");
      setModelOpen(true);
      return;
    }
    if (busy || !open) return;
    if (send({ type: "input", text: message })) change("");
  }

  function cycleMode() {
    if (!mode || !open) return;
    const next = nextMode(mode);
    if (send({ type: "mode", mode: next })) setAsked((queue) => [...queue, next]);
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
      if (event.key === "Tab" && !event.shiftKey) {
        event.preventDefault();
        change(matches[selected].cmd + " ");
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        run(matches[selected].cmd);
        return;
      }
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        setDismissed(true);
        return;
      }
    }

    if (event.key === "Tab" && event.shiftKey && mode) {
      event.preventDefault();
      cycleMode();
      return;
    }

    if (event.key === "Enter" && (event.metaKey || event.ctrlKey || !event.shiftKey)) {
      event.preventDefault();
      run(text);
    }
  }

  const canSend = open && !!text.trim();

  return (
    <div ref={box} className="relative mx-auto w-full max-w-[760px] shrink-0 px-6 pb-3.5">
      {matches.length > 0 && (
        <SlashMenu
          commands={matches}
          selected={Math.min(selected, matches.length - 1)}
          onHover={setSelected}
          onPick={(command) => run(command.cmd)}
        />
      )}
      {modelOpen && <ModelMenu onClose={() => setModelOpen(false)} />}

      <div className="rounded-[18px] border border-border-strong bg-raised pb-2.5 pl-4 pr-3 pt-3">
        <textarea
          ref={input}
          rows={1}
          autoFocus
          value={text}
          onChange={(event) => change(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            empty
              ? "Ask loom to inspect, plan or change something…"
              : "Reply to loom, or type / for commands"
          }
          className="block max-h-[220px] min-h-[26px] w-full resize-none bg-transparent py-0.5 text-[15px] leading-[1.6] outline-none placeholder:text-dim focus-visible:outline-none"
        />
        <div className="mt-2.5 flex items-center gap-1.5">
          <button
            onClick={() => useUi.getState().showPane("files")}
            title="Add files (/add)"
            aria-label="Add files"
            className="flex size-[30px] shrink-0 items-center justify-center rounded-full border border-border-strong text-[17px] leading-none text-subtle hover:bg-bubble"
          >
            +
          </button>
          {mode && (
            <button
              onClick={cycleMode}
              disabled={!open}
              title="Permission mode  ⇧⇥"
              className="flex shrink-0 items-center gap-[7px] whitespace-nowrap rounded-full border border-border-strong px-[11px] py-[5px] text-[12.5px] text-soft enabled:hover:bg-bubble"
            >
              <span className={`size-[7px] rounded-full ${MODES[mode].dot}`} />
              {MODES[mode].label}
              <span className="font-mono text-[10.5px] text-dim">⇧⇥</span>
            </button>
          )}
          <span className="flex-1" />
          {model && (
            <button
              onClick={() => setModelOpen((o) => !o)}
              title={model}
              className="flex min-w-0 items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-[5px] text-[12.5px] text-subtle hover:bg-bubble hover:text-foreground"
            >
              <span className="truncate">{shortModel(model)}</span>
              <span className="text-[9px] text-dim">▼</span>
            </button>
          )}
          {busy ? (
            <button
              onClick={() => send({ type: "cancel" })}
              title="Stop  esc"
              aria-label="Stop"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-foreground hover:opacity-90"
            >
              <span className="size-2.5 rounded-sm bg-background" />
            </button>
          ) : (
            <button
              onClick={() => run(text)}
              disabled={!canSend}
              title="Send  ↵"
              aria-label="Send message"
              className={`flex size-8 shrink-0 items-center justify-center rounded-full text-[16px] font-bold ${
                canSend
                  ? "bg-primary text-on-primary hover:bg-primary-hover"
                  : "bg-border-strong text-dim"
              }`}
            >
              ↑
            </button>
          )}
        </div>
      </div>
      <StatusLine />
    </div>
  );
}
