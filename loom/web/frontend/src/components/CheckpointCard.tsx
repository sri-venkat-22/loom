import { answer, answerSummary, checkpointActions } from "../lib/asks";
import { send } from "../lib/socket";
import type { AskEntry } from "../store/session";

// A /project checkpoint: the phase's document is ready, and the founder approves it,
// asks for changes or stops the run.
export function CheckpointCard({ entry }: { entry: AskEntry }) {
  const { ask } = entry;
  const info = ask.checkpoint;
  const pending = entry.answer === undefined;

  let outcome = null;
  if (!pending) {
    const { text, ok } =
      entry.answer === null
        ? { text: "› aborted, continue with /project run", ok: null }
        : answerSummary(ask, entry.answer, false);
    outcome = <div className={`mt-2 text-[13px] ${ok ? "text-success" : "text-dim"}`}>{text}</div>;
  }

  return (
    <div className="border-l-2 border-primary pl-3">
      <div className="mb-1 text-[12px] text-primary">checkpoint</div>
      {info && (
        <div className="text-muted-foreground">
          <span className="font-bold text-foreground">{info.title}</span> is ready for review:{" "}
          {info.document_title} <span className="text-dim">({info.document})</span>
          {info.verdict && (
            <>
              {" "}
              · verdict <span className="font-bold text-foreground">{info.verdict}</span>
            </>
          )}
        </div>
      )}
      <div className="mt-1 whitespace-pre-wrap break-words">{ask.question}</div>
      {pending && (
        <div className="mt-2 flex flex-wrap gap-x-4 text-[13px]">
          {checkpointActions(ask).map((action) => (
            <button
              key={action.choice.value}
              onClick={() => answer(ask, action.choice.value)}
              title={`${action.key}`}
              className={
                action.primary
                  ? "text-primary hover:underline"
                  : "text-muted-foreground hover:text-foreground"
              }
            >
              {action.label}
            </button>
          ))}
          <button
            onClick={() => send({ type: "cancel" })}
            title="esc"
            className="text-muted-foreground hover:text-destructive"
          >
            Abort
          </button>
        </div>
      )}
      {outcome}
    </div>
  );
}
