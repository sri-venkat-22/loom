import { useState } from "react";

import { answerSummary } from "../lib/asks";
import { send } from "../lib/socket";
import type { AskEntry, Entry } from "../store/session";
import { CheckpointCard } from "./CheckpointCard";
import { DiffView } from "./DiffView";
import { Markdown } from "./Markdown";
import { ToolCard } from "./ToolCard";

type Of<K extends Entry["kind"]> = Extract<Entry, { kind: K }>;

const LEVEL_CLASS = {
  info: "text-dim",
  warning: "text-warning",
  error: "text-destructive",
};

function UserMessage({ entry }: { entry: Of<"user"> }) {
  return (
    <div>
      <div className="mb-1 text-[12px] text-dim">user</div>
      <div className="whitespace-pre-wrap break-words text-muted-foreground">{entry.text}</div>
    </div>
  );
}

function Thinking({ text, active }: { text: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-1 text-[13px] text-dim">
      <button onClick={() => setOpen((o) => !o)} className="hover:text-foreground">
        {active ? <span className="blink">∴ Thinking…</span> : "∴ Thinking"} {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="ml-4 mt-1 whitespace-pre-wrap break-words border-l border-border pl-3">
          {text}
        </div>
      )}
    </div>
  );
}

function LoomMessage({ entry }: { entry: Of<"loom"> }) {
  return (
    <div>
      <div className="mb-1 text-[12px] text-primary">loom</div>
      {entry.reasoning && (
        <Thinking text={entry.reasoning} active={entry.streaming && !entry.text} />
      )}
      {(entry.text || !entry.reasoning) && (
        <div className="text-[16px] leading-6">
          <Markdown text={entry.text} />
          {entry.streaming && <span className="blink text-primary">▍</span>}
        </div>
      )}
    </div>
  );
}

function SystemMessage({ entry }: { entry: Of<"system"> }) {
  return (
    <pre
      className={`whitespace-pre-wrap break-words font-mono text-[13px] ${LEVEL_CLASS[entry.level]}`}
    >
      {entry.text}
    </pre>
  );
}

function PromptAnswer({ askId, initial }: { askId: string; initial: string }) {
  const [text, setText] = useState(initial);
  return (
    <input
      autoFocus
      value={text}
      onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          send({ type: "answer", ask_id: askId, value: text });
        }
      }}
      placeholder="type an answer, then ↵"
      className="mt-2 w-full border-b border-border bg-transparent pb-1 outline-none placeholder:text-dim"
    />
  );
}

function AskMessage({ entry }: { entry: Of<"ask"> }) {
  const { ask, answer } = entry;
  const pending = answer === undefined;

  return (
    <div className="border-l-2 border-primary pl-3">
      <div className="mb-1 text-[12px] text-primary">{ask.kind}</div>
      <div className="whitespace-pre-wrap break-words">{ask.question}</div>
      {ask.subject && (
        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[13px] text-muted-foreground">
          {ask.subject}
        </pre>
      )}
      {pending && ask.kind === "prompt" && (
        <PromptAnswer askId={ask.ask_id} initial={ask.default} />
      )}
      {pending && ask.kind !== "prompt" && (
        <div className="mt-2 flex flex-wrap gap-x-4 text-[13px]">
          {ask.choices.map((choice) => (
            <button
              key={choice.value}
              onClick={() => send({ type: "answer", ask_id: ask.ask_id, value: choice.value })}
              className={
                choice.value === ask.default
                  ? "text-primary hover:underline"
                  : "text-muted-foreground hover:text-foreground"
              }
            >
              {choice.label}
            </button>
          ))}
        </div>
      )}
      {!pending && (
        <div className="mt-2 text-[13px] text-dim">{answerSummary(ask, answer, false).text}</div>
      )}
    </div>
  );
}

// The document itself is edited in the side pane
function EditMessage({ entry }: { entry: Of<"ask"> }) {
  const { ask, answer } = entry;
  return (
    <div className="border-l-2 border-primary pl-3 text-[13px]">
      <div className="mb-1 text-[12px] text-primary">edit</div>
      {answer === undefined ? (
        <div className="text-muted-foreground">
          Editing {ask.subject} in the side pane: <span className="text-foreground">⌘S</span> saves
        </div>
      ) : (
        <div className="text-dim">
          {ask.subject} {answerSummary(ask, answer, false).text}
        </div>
      )}
    </div>
  );
}

export function Message({ entry, asks }: { entry: Entry; asks: AskEntry[] }) {
  switch (entry.kind) {
    case "user":
      return <UserMessage entry={entry} />;
    case "loom":
      return <LoomMessage entry={entry} />;
    case "system":
      return <SystemMessage entry={entry} />;
    case "tool":
      return <ToolCard entry={entry} asks={asks} />;
    case "diff":
      return (
        <div className="text-[14px]">
          <div className="text-muted-foreground">{entry.diff.file}</div>
          <DiffView diff={entry.diff} />
        </div>
      );
    case "ask":
      if (entry.ask.kind === "checkpoint") return <CheckpointCard entry={entry} />;
      if (entry.ask.kind === "edit") return <EditMessage entry={entry} />;
      return <AskMessage entry={entry} />;
  }
}
