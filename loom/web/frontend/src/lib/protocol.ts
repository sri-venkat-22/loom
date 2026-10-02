// The messages loom's server and this frontend send each other over /ws. The server's copy,
// with what each one means, is loom/web/backend/protocol.py; a test checks the two match.

export const PROTOCOL_VERSION = 1;

export type Level = "info" | "warning" | "error";
export type AskKind = "confirm" | "permission" | "choice" | "prompt";
export type TurnStatus = "done" | "cancelled";

export interface Command {
  cmd: string;
  desc: string;
}

export interface SessionEvent {
  type: "session";
  version: string;
  model: string;
  weak_model: string;
  edit_format: string;
  cwd: string;
  files: string[];
  read_only_files: string[];
  commands: Command[];
  tokens: { sent: number; received: number };
  cost: number;
  phase: string | null;
  busy: boolean;
}

export interface UserEvent {
  type: "user";
  text: string;
}

export interface TurnStartEvent {
  type: "turn_start";
  turn_id: string;
}

export interface TurnEndEvent {
  type: "turn_end";
  turn_id: string;
  status: TurnStatus;
}

export interface AssistantDeltaEvent {
  type: "assistant_delta";
  id: string;
  // The answer and the model's thinking: appended, or replacing both when replace
  text: string;
  reasoning: string;
  replace: boolean;
}

export interface AssistantEndEvent {
  type: "assistant_end";
  id: string;
}

export interface SystemEvent {
  type: "system";
  level: Level;
  text: string;
}

export type ToolStatus = "done" | "failed";
export type LineStyle = "done" | "warning" | "error" | "bold" | "dim" | null;

export interface ToolStartEvent {
  type: "tool_start";
  id: string;
  name: string;
  detail: string;
  // The call's arguments as pretty JSON, or null
  args: string | null;
}

export interface ToolOutputEvent {
  type: "tool_output";
  id: string;
  lines: string[];
  error: boolean;
  styles: LineStyle[];
}

export interface ToolEndEvent {
  type: "tool_end";
  id: string;
  status: ToolStatus;
  // What the model got back
  output: string;
}

export interface DiffLine {
  kind: "add" | "del" | "ctx" | "gap" | "note";
  old: number | null;
  new: number | null;
  text: string;
}

export interface DiffEvent {
  type: "diff";
  id: string;
  // The card it belongs on, or null
  tool_id: string | null;
  file: string;
  lines: DiffLine[];
}

export interface Choice {
  value: string;
  label: string;
}

export interface AskEvent {
  type: "ask";
  ask_id: string;
  kind: AskKind;
  question: string;
  choices: Choice[];
  default: string;
  // What it's about, like a command to run
  subject: string | null;
  // The card of the tool call that needs the answer
  tool_id: string | null;
}

export interface AskResolvedEvent {
  type: "ask_resolved";
  ask_id: string;
  // null when the question was dropped, by Esc or loom stopping
  value: string | null;
}

export type ServerEvent =
  | SessionEvent
  | UserEvent
  | TurnStartEvent
  | TurnEndEvent
  | AssistantDeltaEvent
  | AssistantEndEvent
  | SystemEvent
  | ToolStartEvent
  | ToolOutputEvent
  | ToolEndEvent
  | DiffEvent
  | AskEvent
  | AskResolvedEvent;

export type ClientEvent =
  | { type: "input"; text: string }
  | { type: "answer"; ask_id: string; value: string }
  | { type: "cancel" };
