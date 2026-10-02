import { useEffect } from "react";

import { Chat } from "./components/Chat";
import { Composer } from "./components/Composer";
import { PhaseBreadcrumb } from "./components/PhaseBreadcrumb";
import { SidePane } from "./components/SidePane";
import { StatusLine } from "./components/StatusLine";
import { answerWithKey } from "./lib/asks";
import { send } from "./lib/socket";
import { useSession } from "./store/session";
import { useUi } from "./store/ui";

export function App() {
  // Esc stops loom's current work, like in the terminal; ⌘B shows the side pane and ⌘K
  // the commands
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === "b") {
        event.preventDefault();
        useUi.getState().togglePane();
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
      <header className="flex h-[57px] shrink-0 items-center border-b border-border px-[30px]">
        <span className="mr-9 shrink-0 text-[18px] font-bold text-primary">loom</span>
        <PhaseBreadcrumb />
        <button
          onClick={() => useUi.getState().togglePane()}
          title="side pane: files, memory, terminal, model"
          className="ml-auto shrink-0 pl-4 text-muted-foreground hover:text-foreground"
        >
          ⌘B
        </button>
      </header>
      <div className="flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <Chat />
          <Composer />
          <StatusLine />
        </main>
        <SidePane />
      </div>
    </div>
  );
}
