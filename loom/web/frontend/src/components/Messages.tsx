import { useState } from "react";

import { answer, answerSummary } from "../lib/asks";
import { send } from "../lib/socket";
import type { AskEntry, Entry } from "../store/session";
import { ActionButton } from "./Buttons";
import { CheckpointCard } from "./CheckpointCard";
import { DiffView } from "./DiffView";
import { Markdown } from "./Markdown";
import { ToolCard } from "./ToolCard";

type Of<K extends Entry["kind"]> = Extract<Entry, { kind: K }>;

const LEVEL_CLASS = {
  info: "text-muted-foreground",
  warning: "text-warning",
  error: "text-destructive",
};

function UserMessage({ entry }: { entry: Of<"user"> }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-[18px_18px_4px_18px] bg-bubble px-4 py-2.5 text-[15px] leading-[1.55]">
        {entry.text}
      </div>
    </div>
  );
}

function Thinking({ text, active }: { text: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="self-start text-[13px] text-dim hover:text-soft"
      >
        {active ? <span className="blink">∴ Thinking…</span> : "∴ Thought"}{" "}
        <span className="text-[10px]">{open ? "▼" : "▶"}</span>
      </button>
      {open && (
        <div className="whitespace-pre-wrap break-words border-l-2 border-border-strong py-0.5 pl-3 text-[13.5px] leading-[1.6] text-muted-foreground">
          {text}
        </div>
      )}
    </div>
  );
}

function LoomMessage({ entry }: { entry: Of<"loom"> }) {
  return (
    <div className="flex flex-col gap-2">
      {entry.reasoning && (
        <Thinking text={entry.reasoning} active={entry.streaming && !entry.text} />
      )}
      {(entry.text || !entry.reasoning) && (
        <div className="text-[15px] leading-[1.65]">
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
      className={`m-0 whitespace-pre-wrap break-words font-mono text-[13px] leading-normal ${LEVEL_CLASS[entry.level]}`}
    >
      {entry.text}
    </pre>
  );
}

function PromptAnswer({ askId, initial }: { askId: string; initial: string }) {
  const [text, setText] = useState(initial);
  const submit = () => send({ type: "answer", ask_id: askId, value: text });
  return (
    <div className="mt-3 flex gap-2">
      <input
        autoFocus
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            submit();
          }
        }}
        placeholder="Type an answer"
        className="min-w-0 flex-1 rounded-full border border-border-strong bg-background px-3.5 py-2 text-[14px] outline-none placeholder:text-dim"
      />
      <button
        onClick={submit}
        className="rounded-full bg-primary px-4 text-[13px] font-semibold text-on-primary hover:bg-primary-hover"
      >
        Send
      </button>
    </div>
  );
}

// A question loom asks that isn't about a tool call, like a confirm or a free-text prompt
function AskMessage({ entry }: { entry: Of<"ask"> }) {
  const { ask, answer: given } = entry;
  const pending = given === undefined;

  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3">
      <div className="flex items-center gap-2.5">
        {pending ? (
          <span className="blink size-2 shrink-0 rounded-full bg-primary" />
        ) : (
          <span className="text-[13px] text-dim">›</span>
        )}
        <div className="min-w-0 whitespace-pre-wrap break-words text-[14px]">{ask.question}</div>
      </div>
      {ask.subject && (
        <pre className="mt-2.5 whitespace-pre-wrap break-words rounded-lg border border-border bg-background px-3 py-2 font-mono text-[12.5px] text-soft">
          {ask.subject}
        </pre>
      )}
      {pending && ask.kind === "prompt" && (
        <PromptAnswer askId={ask.ask_id} initial={ask.default} />
      )}
      {pending && ask.kind !== "prompt" && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {ask.choices.map((choice) => (
            <ActionButton
              key={choice.value}
              variant={choice.value === ask.default ? "primary" : "secondary"}
              hint={choice.value[0]}
              onClick={() => answer(ask, choice.value)}
            >
              {choice.label}
            </ActionButton>
          ))}
        </div>
      )}
      {!pending && (
        <div className="mt-1.5 pl-[18px] text-[13px] text-dim">
          {answerSummary(ask, given, false).text}
        </div>
      )}
    </div>
  );
}

// The document itself is edited in the side pane
function EditMessage({ entry }: { entry: Of<"ask"> }) {
  const { ask, answer: given } = entry;
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-border bg-card px-4 py-3 text-[13.5px]">
      {given === undefined ? (
        <>
          <span className="blink size-2 shrink-0 rounded-full bg-primary" />
          <span className="text-muted-foreground">
            Editing <span className="font-mono text-[13px] text-foreground">{ask.subject}</span> in
            the side pane · <span className="text-foreground">⌘S</span> saves
          </span>
        </>
      ) : (
        <span className="text-dim">
          <span className="font-mono text-[13px]">{ask.subject}</span>{" "}
          {answerSummary(ask, given, false).text}
        </span>
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
        <div>
          <div className="mb-1 font-mono text-[13px] text-muted-foreground">{entry.diff.file}</div>
          <DiffView diff={entry.diff} />
        </div>
      );
    case "ask":
      if (entry.ask.kind === "checkpoint") return <CheckpointCard entry={entry} />;
      if (entry.ask.kind === "edit") return <EditMessage entry={entry} />;
      return <AskMessage entry={entry} />;
  }
}
