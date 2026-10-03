import { useEffect, useState } from "react";

import { api } from "../lib/api";
import { answer, answerSummary, checkpointActions } from "../lib/asks";
import { send } from "../lib/socket";
import type { AskEntry } from "../store/session";
import { useUi } from "../store/ui";
import { ActionButton } from "./Buttons";

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

// Lines of the document shown on the card
const EXCERPT_LINES = 8;

// The start of the phase's document, or null if it can't be read
function useExcerpt(path: string | undefined) {
  const [lines, setLines] = useState<string[] | null>(null);
  useEffect(() => {
    if (!path) return;
    let live = true;
    api
      .file(path)
      .then((file) => live && setLines(file.text.split("\n").slice(0, EXCERPT_LINES)))
      .catch(() => live && setLines(null));
    return () => {
      live = false;
    };
  }, [path]);
  return lines;
}

// A /project checkpoint: the phase's document is ready, and the founder approves it,
// asks for changes or stops the run.
export function CheckpointCard({ entry }: { entry: AskEntry }) {
  const { ask } = entry;
  const info = ask.checkpoint;
  const pending = entry.answer === undefined;
  const excerpt = useExcerpt(info?.document);
  const openInViewer = useUi((state) => state.openInViewer);

  let outcome = null;
  if (!pending) {
    const { text, ok } =
      entry.answer === null
        ? { text: "Aborted · continue with /project run", ok: null }
        : answerSummary(ask, entry.answer, false);
    const shown = text.replace(/^› /, "");
    outcome = (
      <div className={`text-[13px] ${ok ? "text-success" : "text-muted-foreground"}`}>
        {ok ? "✓ " : ""}
        {capitalize(shown)}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-[14px] border border-primary/35 bg-primary/5 px-[18px] py-4">
      <div className="flex items-center gap-2.5">
        <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-primary">
          Checkpoint{info ? ` · ${info.title}` : ""}
        </span>
        {info?.verdict && (
          <span className="ml-auto rounded-full bg-success/15 px-2 py-0.5 font-mono text-[11px] font-semibold text-add">
            verdict {info.verdict}
          </span>
        )}
      </div>
      {info && (
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-[17px] font-semibold">
            {capitalize(info.document_title)} is ready for review
          </span>
          <button
            onClick={() => openInViewer(info.document)}
            className="rounded-md border border-border-strong bg-raised px-2 py-0.5 font-mono text-[12px] text-soft hover:bg-bubble"
          >
            {info.document} ↗
          </button>
        </div>
      )}
      {excerpt && (
        <div className="rounded-[10px] border border-border bg-background px-3.5 py-3 font-mono text-[12.5px] leading-[1.6]">
          {excerpt.map((line, i) => (
            <div
              key={i}
              // A row per line, so the card stays short; the document is a click away
              className={`min-h-5 truncate ${line.startsWith("#") ? "text-primary" : "text-soft"}`}
            >
              {line}
            </div>
          ))}
        </div>
      )}
      <div className="whitespace-pre-wrap break-words text-[14px] text-soft">{ask.question}</div>
      {pending && (
        <div className="flex flex-wrap items-center gap-2">
          {checkpointActions(ask).map((action) => (
            <ActionButton
              key={action.choice.value}
              variant={action.primary ? "primary" : "secondary"}
              hint={action.key}
              onClick={() => answer(ask, action.choice.value)}
              className="px-3.5 py-[7px]"
            >
              {action.label}
            </ActionButton>
          ))}
          <ActionButton
            variant="danger"
            hint="esc"
            title="Stop the run"
            onClick={() => send({ type: "cancel" })}
            className="ml-auto"
          >
            Abort
          </ActionButton>
        </div>
      )}
      {outcome}
    </div>
  );
}
