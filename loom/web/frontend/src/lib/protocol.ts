// The messages loom's server and this frontend send each other over /ws. The server's copy,
// with what each one means, is loom/web/backend/protocol.py; a test checks the two match.

export const PROTOCOL_VERSION = 1;

export type Level = "info" | "warning" | "error";
export type AskKind = "confirm" | "permission" | "choice" | "checkpoint" | "prompt" | "edit";
export type PhaseStatus = "pending" | "running" | "review" | "approved";
export type PermissionMode = "ask" | "accept-edits" | "plan" | "bypass";

export interface Phase {
  key: string;
  title: string;
  status: PhaseStatus;
}

// The /project phase a checkpoint reviews
export interface Checkpoint {
  phase: string;
  title: string;
  document: string;
  document_title: string;
  verdict: string | null;
  next: string | null;
  next_title: string | null;
}
export type TurnStatus = "done" | "cancelled";

// The /project dashboard's timeline

export type RunOutcome = "done" | "stopped" | "failed";

// What runs of a phase's agent added up to, or the whole project's
export interface Metrics {
  runs: number;
  seconds: number;
  cost: number;
  tokens_sent: number;
  tokens_received: number;
  commits: number;
}

// One run of a phase's agent. base and head are the commits HEAD was at before and after.
export interface RunEntry {
  run: number;
  started: string;
  finished: string;
  seconds: number;
  cost: number;
  tokens_sent: number;
  tokens_received: number;
  base: string | null;
  head: string | null;
  commits: number;
  verdict: string | null;
  outcome: RunOutcome;
}

export interface Decision {
  id: number;
  time: string;
  phase: string | null;
  source: string;
  kind: string;
  text: string;
  reason: string | null;
}

export interface HistoryEntry {
  time: string;
  phase?: string;
  event: string;
  note?: string;
}

export interface PhaseTimeline {
  key: string;
  number: number;
  title: string;
  agent: string;
  document: string;
  document_title: string;
  produces: string;
  status: PhaseStatus;
  // Pending again because an earlier phase was redone
  stale: boolean;
  verdict: string | null;
  // The phase the project is in
  current: boolean;
  metrics: Metrics;
  run_log: RunEntry[];
  // How often a failing test report sent the project back to it
  fix_rounds: number;
  decisions: Decision[];
  history: HistoryEntry[];
}

export interface Timeline {
  // False without a project
  available: boolean;
  idea: string | null;
  created: string | null;
  // The phase it's in, or null when it's complete
  current: string | null;
  complete: boolean;
  totals: Metrics | null;
  phases: PhaseTimeline[];
  // The decisions of no phase
  decisions: Decision[];
}

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
  // The /project idea, or null without a project
  project: string | null;
  // The phase the project is in, or null when there's none or it's complete
  phase: string | null;
  phases: Phase[];
  // The id of the saved conversation the chat is
  conversation: string | null;
  // The git branch, or null outside a repo or on a detached HEAD
  branch: string | null;
  // The agent's permission mode, or null when the coder has none
  permission_mode: PermissionMode | null;
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
  checkpoint: Checkpoint | null;
}

export interface AskResolvedEvent {
  type: "ask_resolved";
  ask_id: string;
  // null when the question was dropped, by Esc or loom stopping
  value: string | null;
}

// The chat is now another conversation, like one /resume continues: clear it, and the
// messages that follow show that one
export interface ConversationEvent {
  type: "conversation";
  id: string;
  title: string;
}

// Output of a command like /run, for the side pane's terminal
export interface TerminalEvent {
  type: "terminal";
  text: string;
  // A new command starts
  start: boolean;
}

// The /project dashboard's view of the project, sent when it changes
export interface TimelineEvent extends Timeline {
  type: "timeline";
}

export type ServerEvent =
  | SessionEvent
  | TimelineEvent
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
  | AskResolvedEvent
  | TerminalEvent
  | ConversationEvent;

export type ClientEvent =
  | { type: "input"; text: string }
  | { type: "answer"; ask_id: string; value: string }
  | { type: "cancel" }
  // Switch the permission mode, like Shift-Tab in the terminal
  | { type: "mode"; mode: PermissionMode };
