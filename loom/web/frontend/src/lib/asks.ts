import { pendingAsk, useSession } from "../store/session";
import type { AskEvent, Choice } from "./protocol";
import { send } from "./socket";

export function answer(ask: AskEvent, value: string) {
  send({ type: "answer", ask_id: ask.ask_id, value });
}

// The choice whose value starts with key: y for yes, n for no, a for always...
export function choiceForKey(ask: AskEvent, key: string): Choice | undefined {
  if (ask.kind === "prompt" || key.length !== 1) return undefined;
  const lower = key.toLowerCase();
  if (ask.kind === "checkpoint") {
    return checkpointActions(ask).find((action) => action.key === lower)?.choice;
  }
  return ask.choices.find((choice) => choice.value.toLowerCase().startsWith(lower));
}

export interface CheckpointAction {
  key: string;
  label: string;
  choice: Choice;
  primary: boolean;
}

const CHECKPOINT_ACTIONS: Record<string, { key: string; label: string; primary?: boolean }> = {
  approve: { key: "a", label: "Approve", primary: true },
  "approve anyway": { key: "a", label: "Approve anyway" },
  "send back": { key: "s", label: "Send back to Building" },
  reject: { key: "r", label: "Request changes" },
};

// The buttons of a checkpoint, in the order shown. Abort is Esc, which stops the run.
export function checkpointActions(ask: AskEvent): CheckpointAction[] {
  const actions: CheckpointAction[] = [];
  for (const choice of ask.choices) {
    const action = CHECKPOINT_ACTIONS[choice.value];
    if (action) actions.push({ ...action, primary: action.primary ?? false, choice });
  }
  return actions.sort((a, b) => Number(b.primary) - Number(a.primary));
}

// Answer the question loom is waiting on with a key press. Returns whether it did.
export function answerWithKey(key: string): boolean {
  const entry = pendingAsk(useSession.getState().entries);
  if (!entry) return false;
  const choice = choiceForKey(entry.ask, key);
  if (!choice) return false;
  answer(entry.ask, choice.value);
  return true;
}

// How a choice reads in a hint line. An edit's yes and no accept or reject its diff.
export function choiceLabel(ask: AskEvent, choice: Choice, onDiff: boolean): string {
  if (ask.kind === "permission" && onDiff) {
    if (choice.value === "yes") return "accept";
    if (choice.value === "no") return "reject";
  }
  return choice.label.charAt(0).toLowerCase() + choice.label.slice(1);
}

// What an answered question shows, and whether the answer let loom go ahead.
export function answerSummary(
  ask: AskEvent,
  value: string | null | undefined,
  onDiff: boolean,
): { text: string; ok: boolean | null } {
  if (value === null || value === undefined) return { text: "cancelled", ok: null };
  if (ask.kind === "permission") {
    const ok = value !== "no";
    if (onDiff) return { text: ok ? "accepted" : "rejected", ok };
    return { text: ok ? "allowed" : "denied", ok };
  }
  if (ask.kind === "checkpoint") {
    const done: Record<string, string> = {
      approve: "approved",
      "approve anyway": "approved anyway",
      "send back": "sent back to Building",
      reject: "changes requested",
      edit: "editing",
    };
    const ok = value === "approve" || value === "approve anyway";
    return { text: `› ${done[value] ?? value}`, ok: ok ? true : null };
  }
  const choice = ask.choices.find((c) => c.value === value);
  return { text: `› ${choice?.label ?? (value || "(empty)")}`, ok: null };
}
