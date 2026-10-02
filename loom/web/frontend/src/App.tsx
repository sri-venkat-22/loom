import { useEffect } from "react";

import { Chat } from "./components/Chat";
import { Composer } from "./components/Composer";
import { PhaseBreadcrumb } from "./components/PhaseBreadcrumb";
import { StatusLine } from "./components/StatusLine";
import { answerWithKey } from "./lib/asks";
import { send } from "./lib/socket";
import { useSession } from "./store/session";

export function App() {
  // Esc stops loom's current work, like in the terminal
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && useSession.getState().session?.busy) {
        event.preventDefault();
        send({ type: "cancel" });
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
      </header>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Chat />
        <Composer />
        <StatusLine />
      </main>
    </div>
  );
}
