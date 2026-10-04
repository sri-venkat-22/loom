import { useEffect } from "react";

import { Chat, Welcome } from "./components/Chat";
import { Composer, Suggestions } from "./components/Composer";
import { PhaseBreadcrumb } from "./components/PhaseBreadcrumb";
import { RewindDialog } from "./components/RewindDialog";
import { SidePane } from "./components/SidePane";
import { Sidebar } from "./components/Sidebar";
import { answerWithKey } from "./lib/asks";
import { changeTotals, useChanges } from "./lib/changes";
import { send } from "./lib/socket";
import { isFresh, useSession } from "./store/session";
import { useUi } from "./store/ui";

// A panel outline with its sidebar on the left or right, for the two toggles
function PanelIcon({ side }: { side: "left" | "right" }) {
  return (
    <span
      className={`flex h-[13px] w-4 rounded-[3px] border-[1.5px] border-muted-foreground ${
        side === "right" ? "justify-end" : ""
      }`}
    >
      <span
        className={`border-muted-foreground ${
          side === "left" ? "w-[5px] border-r-[1.5px]" : "w-1.5 border-l-[1.5px]"
        }`}
      />
    </span>
  );
}

function ChangesButton() {
  const { changes } = useChanges("session");
  const { added, removed } = changeTotals(changes);
  if (!changes?.available) return null;
  return (
    <button
      onClick={() => useUi.getState().showPane("changes")}
      title="Code changes since loom started"
      className="flex shrink-0 items-center gap-2 rounded-full border border-border-strong px-3 py-[5px] text-[13px] hover:bg-raised"
    >
      <span className="text-soft">Changes</span>
      <span className="font-mono text-[12px]">
        <span className="text-add">+{added}</span> <span className="text-del">-{removed}</span>
      </span>
    </button>
  );
}

function Header() {
  const sidebar = useUi((state) => state.sidebar);
  const cwd = useSession((state) => state.session?.cwd ?? "");
  const branch = useSession((state) => state.session?.branch ?? null);
  const project = cwd.split(/[\\/]/).filter(Boolean).pop() ?? "";

  return (
    <header className="flex h-[52px] shrink-0 items-center gap-3.5 border-b border-line px-3.5">
      <button
        onClick={() => useUi.getState().toggleSidebar()}
        title="Sessions  ⌘\"
        aria-label="Toggle the sessions sidebar"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg hover:bg-selected"
      >
        <PanelIcon side="left" />
      </button>
      {!sidebar && <span className="font-mono text-[16px] font-bold text-primary">loom</span>}
      <div className="flex min-w-0 shrink items-center gap-2">
        <span className="truncate text-[14px] font-semibold" title={cwd}>
          {project}
        </span>
        {branch && (
          <span className="shrink-0 rounded-full border border-border-strong px-2 py-0.5 font-mono text-[12px] text-muted-foreground">
            {branch}
          </span>
        )}
      </div>
      <PhaseBreadcrumb />
      <ChangesButton />
      <button
        onClick={() => useUi.getState().togglePane()}
        title="Side pane  ⌘B"
        aria-label="Toggle the side pane"
        className="flex size-8 shrink-0 items-center justify-center rounded-lg hover:bg-selected"
      >
        <PanelIcon side="right" />
      </button>
    </header>
  );
}

export function App() {
  // Until the first request, a new conversation shows the welcome, not loom's banner
  const empty = useSession((state) => isFresh(state.entries));

  // Esc stops loom's current work, like in the terminal; ⌘\ shows the sessions, ⌘B the
  // side pane and ⌘K the commands
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === "b") {
        event.preventDefault();
        useUi.getState().togglePane();
        return;
      }
      if (mod && event.key === "\\") {
        event.preventDefault();
        useUi.getState().toggleSidebar();
        return;
      }
      if (mod && event.key.toLowerCase() === "k") {
        event.preventDefault();
        useUi.getState().openPalette();
        return;
      }
      if (event.key === "Escape") {
        if (useSession.getState().session?.busy) {
          event.preventDefault();
          send({ type: "cancel" });
        } else if (useUi.getState().pane) {
          useUi.getState().showPane(null);
        }
        return;
      }
      // Outside the inputs, keys answer loom's question, like y and n for a diff
      const target = event.target as HTMLElement | null;
      const typing = target?.tagName === "TEXTAREA" || target?.tagName === "INPUT";
      if (!typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
        if (answerWithKey(event.key)) event.preventDefault();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="relative flex h-screen flex-col overflow-hidden bg-background text-foreground">
      <Header />
      <div className="flex min-h-0 flex-1">
        <Sidebar />
        <main className="flex min-h-0 min-w-[340px] flex-1 flex-col">
          {empty ? <Welcome /> : <Chat />}
          <Composer />
          {empty && <Suggestions />}
        </main>
        <SidePane />
      </div>
      <RewindDialog />
    </div>
  );
}
