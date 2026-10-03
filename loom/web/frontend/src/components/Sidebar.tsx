import { useEffect, useState } from "react";

import { type SavedSession, api } from "../lib/api";
import { send } from "../lib/socket";
import { useSession } from "../store/session";
import { useUi } from "../store/ui";

// How long ago an ISO time was, briefly: "just now", "5m", "3h", "2d", or the date.
export function ago(iso: string | null, now = Date.now()) {
  if (!iso) return "now";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (now - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)}d ago`;
  return iso.slice(0, 10);
}

const CONNECTION = {
  open: { dot: "bg-success", label: "connected" },
  connecting: { dot: "bg-warning", label: "connecting" },
  closed: { dot: "bg-destructive", label: "disconnected" },
};

function SessionItem({
  session,
  busy,
  idle,
}: {
  session: SavedSession;
  busy: boolean;
  idle: boolean;
}) {
  const current = session.current;
  return (
    <button
      disabled={current || !idle}
      onClick={() => send({ type: "input", text: `/resume ${session.id}` })}
      title={current ? "the current conversation" : `/resume ${session.id}`}
      className={`w-full rounded-[10px] px-3 py-[9px] text-left ${
        current ? "bg-selected" : "enabled:hover:bg-card"
      }`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`min-w-0 flex-1 truncate text-[13.5px] ${current ? "text-foreground" : "text-subtle"}`}
        >
          {session.title || "New conversation"}
        </span>
        {current && busy && <span className="blink size-[7px] rounded-full bg-primary" />}
      </div>
      <div className={`mt-0.5 text-[12px] ${current ? "text-muted-foreground" : "text-dim"}`}>
        {ago(session.updated)}
        {session.messages > 0 &&
          ` · ${session.messages} message${session.messages === 1 ? "" : "s"}`}
      </div>
    </button>
  );
}

// The project's saved conversations, on the left like Claude Code's sessions. Choosing
// one continues it, with loom's own /resume; "New session" starts one, with /clear.
export function Sidebar() {
  const open = useUi((state) => state.sidebar);
  const conversation = useSession((state) => state.session?.conversation ?? null);
  const cwd = useSession((state) => state.session?.cwd ?? "");
  const busy = useSession((state) => state.session?.busy ?? false);
  const connection = useSession((state) => state.connection);
  const connected = connection === "open";
  const [sessions, setSessions] = useState<SavedSession[]>([]);
  const [saved, setSaved] = useState(true);
  const [error, setError] = useState("");
  const idle = connected && !busy;

  // Read the list again when the conversation changes, and after each request saves it
  useEffect(() => {
    if (!open || !connected) return;
    let live = true;
    api
      .sessions()
      .then((found) => {
        if (!live) return;
        setSessions(found.sessions);
        setSaved(found.saved);
        setError("");
      })
      .catch((err: Error) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, [open, connected, conversation, busy]);

  if (!open) return null;

  const project = cwd.split(/[\\/]/).filter(Boolean).pop() ?? "";
  const status = CONNECTION[connection];

  return (
    <aside className="flex w-[264px] shrink-0 flex-col border-r border-line bg-sidebar">
      <div className="flex items-center px-3.5 pb-2.5 pt-3.5">
        <span className="font-mono text-[17px] font-bold text-primary">loom</span>
      </div>
      <div className="px-2.5 pb-3">
        <button
          disabled={!idle}
          onClick={() => send({ type: "input", text: "/clear" })}
          title="Start a new conversation (/clear)"
          className="flex w-full items-center gap-2.5 rounded-[10px] border border-border bg-card px-3 py-[9px] text-left text-[13.5px] enabled:hover:bg-selected disabled:opacity-60"
        >
          <span className="flex size-[18px] items-center justify-center rounded-full bg-primary text-[14px] font-bold leading-none text-on-primary">
            +
          </span>
          New session
        </button>
      </div>
      <div className="px-[22px] py-1.5 text-[12px] text-dim">Recents</div>
      <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2.5 pb-2.5">
        {error && <div className="px-3 text-[13px] text-destructive">{error}</div>}
        {!saved && (
          <div className="px-3 text-[13px] text-dim">
            Conversations aren't being saved (--no-sessions).
          </div>
        )}
        {sessions.map((session) => (
          <SessionItem key={session.id} session={session} busy={busy} idle={idle} />
        ))}
      </div>
      <div className="m-2.5 flex flex-col gap-1 rounded-xl border border-line p-3">
        <div className="truncate text-[13px] font-semibold">{project || "loom"}</div>
        {cwd && (
          // Long paths lose their start, keeping the project's own name in view
          <div
            className="truncate text-left font-mono text-[11.5px] text-dim [direction:rtl]"
            title={cwd}
          >
            &lrm;{cwd}&lrm;
          </div>
        )}
        <div className="mt-1 flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <span className={`size-1.5 rounded-full ${status.dot}`} />
          {status.label} · {window.location.host}
        </div>
      </div>
    </aside>
  );
}
