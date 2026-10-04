import { useState } from "react";

import { answer, answerSummary, keepPlanning, planActions } from "../lib/asks";
import { send } from "../lib/socket";
import type { AskEntry } from "../store/session";
import { ActionButton } from "./Buttons";
import { Markdown } from "./Markdown";

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

// The plan the agent made in plan mode: approve it (auto-accepting edits or asking for
// each), edit it in the side pane, or keep planning with what to change.
export function PlanCard({ entry }: { entry: AskEntry }) {
  const { ask } = entry;
  const plan = ask.plan;
  const pending = entry.answer === undefined;
  const [feedback, setFeedback] = useState("");
  // An answered plan folds away; the newest one stays open
  const [open, setOpen] = useState(true);
  const actions = planActions(ask);
  const keep = actions.find((action) => action.key === "k");

  let outcome = null;
  if (!pending) {
    const { text, ok } =
      entry.answer === null
        ? { text: "Stopped", ok: null }
        : answerSummary(ask, entry.answer, false);
    outcome = (
      <div className={`text-[13px] ${ok ? "text-success" : "text-muted-foreground"}`}>
        {ok ? "✓ " : ""}
        {capitalize(text.replace(/^› /, ""))}
      </div>
    );
  }
  const shown = pending || open;

  return (
    <div
      aria-label="Plan"
      className="flex flex-col gap-3 rounded-[14px] border border-primary/35 bg-primary/5 px-[18px] py-4"
    >
      <div className="flex items-center gap-2.5">
        <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-primary">
          Plan
        </span>
        {plan?.path && (
          <span className="min-w-0 truncate font-mono text-[12px] text-dim">{plan.path}</span>
        )}
        {!pending && (
          <button
            onClick={() => setOpen((value) => !value)}
            className="ml-auto shrink-0 text-[12px] text-dim hover:text-soft"
          >
            {open ? "Hide ▼" : "Show ▶"}
          </button>
        )}
      </div>
      {plan && shown && (
        <div className="scroll-thin max-h-[520px] overflow-y-auto rounded-[10px] border border-border bg-background px-4 py-3 text-[14.5px] leading-[1.6]">
          <Markdown text={plan.text} />
        </div>
      )}
      {pending && (
        <>
          <div className="text-[14px] text-soft">{ask.question}</div>
          <div className="flex flex-wrap items-center gap-2">
            {actions
              .filter((action) => action.key !== "k")
              .map((action) => (
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
              title="Stop the agent"
              onClick={() => send({ type: "cancel" })}
              className="ml-auto"
            >
              Stop
            </ActionButton>
          </div>
          {keep && (
            <div className="flex gap-2">
              <input
                value={feedback}
                onChange={(event) => setFeedback(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    keepPlanning(ask, feedback);
                  }
                }}
                placeholder="What should change? (optional)"
                aria-label="Plan feedback"
                className="min-w-0 flex-1 rounded-lg border border-border-strong bg-background px-3 py-1.5 text-[13.5px] outline-none placeholder:text-dim"
              />
              <ActionButton
                variant="secondary"
                hint={keep.key}
                onClick={() => keepPlanning(ask, feedback)}
              >
                {keep.label}
              </ActionButton>
            </div>
          )}
        </>
      )}
      {outcome}
    </div>
  );
}
