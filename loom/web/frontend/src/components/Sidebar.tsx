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

// The project's saved conversations, on the left like Claude Code's sessions. Choosing
// one continues it, with loom's own /resume; "new" starts one, with /clear.
export function Sidebar() {
  const open = useUi((state) => state.sidebar);
  const conversation = useSession((state) => state.session?.conversation ?? null);
  const cwd = useSession((state) => state.session?.cwd ?? "");
  const busy = useSession((state) => state.session?.busy ?? false);
  const connected = useSession((state) => state.connection === "open");
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

  const project = cwd.split("/").filter(Boolean).pop() ?? "";

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-border text-[13px]">
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="min-w-0 truncate text-foreground" title={cwd}>
          {project}
        </span>
        <button
          disabled={!idle}
          onClick={() => send({ type: "input", text: "/clear" })}
          title="start a new conversation (/clear)"
          className="ml-auto shrink-0 text-primary enabled:hover:underline disabled:text-dim"
        >
          + new
        </button>
      </div>
      <div className="px-4 pb-2 text-[12px] text-dim">sessions</div>
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto pb-4">
        {error && <div className="px-4 text-destructive">{error}</div>}
        {!saved && (
          <div className="px-4 text-dim">Conversations aren't being saved (--no-sessions).</div>
        )}
        {sessions.map((session) => (
          <button
            key={session.id}
            disabled={session.current || !idle}
            onClick={() => send({ type: "input", text: `/resume ${session.id}` })}
            title={session.current ? "the current conversation" : `/resume ${session.id}`}
            className={`block w-full border-l-2 px-4 py-2 text-left ${
              session.current
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground enabled:hover:text-foreground"
            }`}
          >
            <div className="truncate">{session.title || "New conversation"}</div>
            <div className="mt-0.5 text-[12px] text-dim">
              {ago(session.updated)}
              {session.messages > 0 && ` · ${session.messages} msgs`}
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
