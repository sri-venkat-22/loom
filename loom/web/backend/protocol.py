"""The messages loom's web UI and its server send each other over the /ws WebSocket.

Every message is a JSON object whose "type" is one of the names below. The browser's copy
of this list is loom/web/frontend/src/lib/protocol.ts, and a test checks the two match.

Server to browser:

  session        {version, model, weak_model, edit_format, cwd, files, read_only_files,
                  commands: [{cmd, desc}], tokens: {sent, received}, cost, phase, busy}
                 The current state. Sent first on every connection and again when it changes.
  user           {text}                     A message the user sent, as loom received it.
  turn_start     {turn_id}                  loom started working on the user's message.
  turn_end       {turn_id, status}          It stopped: status is done or cancelled.
  assistant_delta {id, text, reasoning, replace}
                 Streamed text of assistant message id: its answer and its thinking,
                 appended, or replacing both when replace.
  assistant_end  {id}                       That message is complete.
  system         {level, text}              A line of loom output: info, warning or error.
  ask            {ask_id, kind, question, choices: [{value, label}], default}
                 loom waits for an answer. kind is confirm, permission, choice or prompt;
                 a prompt's answer is free text and default prefills it.
  ask_resolved   {ask_id, value}            The question was answered.

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
    "ask",
    "ask_resolved",
)

CLIENT_EVENTS = (
    "input",
    "answer",
    "cancel",
)

LEVELS = ("info", "warning", "error")
ASK_KINDS = ("confirm", "permission", "choice", "prompt")
TURN_STATUSES = ("done", "cancelled")


def event(type, **payload):
    """A server-to-browser message."""
    if type not in SERVER_EVENTS:
        raise ValueError(f"Unknown event type: {type}")
    return dict(type=type, **payload)
