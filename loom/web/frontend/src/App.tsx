import { useEffect } from "react";

import { Chat } from "./components/Chat";
import { Composer } from "./components/Composer";
import { StatusLine } from "./components/StatusLine";
import { send } from "./lib/socket";
import { useSession } from "./store/session";

export function App() {
  // Esc stops loom's current work, like in the terminal
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && useSession.getState().session?.busy) {
        event.preventDefault();
        send({ type: "cancel" });
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="relative flex h-screen flex-col overflow-hidden bg-background text-foreground">
      <header className="flex h-[57px] shrink-0 items-center border-b border-border px-[30px]">
        <span className="mr-9 text-[18px] font-bold text-primary">loom</span>
      </header>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Chat />
        <Composer />
        <StatusLine />
      </main>
    </div>
  );
}
