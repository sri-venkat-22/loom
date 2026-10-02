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
  return ask.choices.find((choice) => choice.value.toLowerCase().startsWith(lower));
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
  if (value === null) return { text: "cancelled", ok: null };
  if (ask.kind === "permission") {
    const ok = value !== "no";
    if (onDiff) return { text: ok ? "accepted" : "rejected", ok };
    return { text: ok ? "allowed" : "denied", ok };
  }
  const choice = ask.choices.find((c) => c.value === value);
  return { text: `› ${choice?.label ?? (value || "(empty)")}`, ok: null };
}
