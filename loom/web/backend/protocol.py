"""The messages loom's web UI and its server send each other over the /ws WebSocket.

Every message is a JSON object whose "type" is one of the names below. The browser's copy
of this list is loom/web/frontend/src/lib/protocol.ts, and a test checks the two match.

Server to browser:

  session        {version, model, weak_model, edit_format, cwd, files, read_only_files,
                  commands: [{cmd, desc}], tokens: {sent, received}, cost, project,
                  phase, phases: [{key, title, status}], busy}
                 The current state. Sent first on every connection and again when it changes.
                 project is the /project idea (null without one), phase the key of the
                 phase it's in (null when there's none or it's complete), and each phase's
                 status pending, running, review or approved.
  user           {text}                     A message the user sent, as loom received it.
  turn_start     {turn_id}                  loom started working on the user's message.
  turn_end       {turn_id, status}          It stopped: status is done or cancelled.
  assistant_delta {id, text, reasoning, replace}
                 Streamed text of assistant message id: its answer and its thinking,
                 appended, or replacing both when replace.
  assistant_end  {id}                       That message is complete.
  system         {level, text}              A line of loom output: info, warning or error.
  tool_start     {id, name, detail, args}   The agent called a tool: its card shows
                                            name(detail), and args (JSON) when expanded.
  tool_output    {id, lines, error, styles} Result lines shown under the card. styles has
                                            done, warning, error, bold, dim or null per line.
  tool_end       {id, status, output}       The call finished: done or failed. output is
                                            what the model got back.
  diff           {id, tool_id, file, lines: [{kind, old, new, text}]}
                 A unified diff, on tool_id's card if it has one. kind is add, del, ctx,
                 gap (between hunks) or note; old and new are line numbers or null.
  ask            {ask_id, kind, question, choices: [{value, label}], default, subject,
                  tool_id, checkpoint}
                 loom waits for an answer. kind is confirm, permission, choice, checkpoint
                 or prompt; a prompt's answer is free text and default prefills it.
                 subject is what it's about, like a command, and tool_id the card it
                 belongs on. A checkpoint reviews a /project phase, and checkpoint is
                 {phase, title, document, document_title, verdict, next, next_title}.
                 An edit asks for the new text of the document at subject, whose text is
                 default.
  ask_resolved   {ask_id, value}            The question was answered.
  terminal       {text, start}              Output of a command like /run, for the side
                                            pane's terminal; start begins a new command.

Browser to server:

  input          {text}                     The user's next message or /command.
  answer         {ask_id, value}            The answer to an ask.
  cancel         {}                         Stop the current work, like Esc in the terminal.

On connecting, the browser gets the session followed by every other message so far, so a
reloaded page shows the whole conversation.
"""

PROTOCOL_VERSION = 1

SERVER_EVENTS = (
    "session",
    "user",
    "turn_start",
    "turn_end",
    "assistant_delta",
    "assistant_end",
    "system",
    "tool_start",
    "tool_output",
    "tool_end",
    "diff",
    "ask",
    "ask_resolved",
    "terminal",
)

CLIENT_EVENTS = (
    "input",
    "answer",
    "cancel",
)

LEVELS = ("info", "warning", "error")
ASK_KINDS = ("confirm", "permission", "choice", "checkpoint", "prompt", "edit")
PHASE_STATUSES = ("pending", "running", "review", "approved")
TURN_STATUSES = ("done", "cancelled")
TOOL_STATUSES = ("done", "failed")
DIFF_LINE_KINDS = ("add", "del", "ctx", "gap", "note")


def event(type, **payload):
    """A server-to-browser message."""
    if type not in SERVER_EVENTS:
        raise ValueError(f"Unknown event type: {type}")
    return dict(type=type, **payload)
