import { useUi } from "../store/ui";
import type { Command } from "./protocol";

// Commands the browser handles itself, without loom: they open parts of the UI.
export const LOCAL_COMMANDS: Command[] = [
  { cmd: "/files", desc: "Open the file tree, to add files to the chat or view them (⌘B)" },
  { cmd: "/memory", desc: "Search the project's shared memory: /memory [QUERY]" },
  { cmd: "/terminal", desc: "Show the output of /run" },
];

// Run text if it's one of LOCAL_COMMANDS. Returns whether it was.
export function runLocal(text: string): boolean {
  const [name, ...rest] = text.trim().split(/\s+/);
  const args = rest.join(" ");
  const ui = useUi.getState();
  switch (name) {
    case "/files":
      ui.showPane("files");
      return true;
    case "/memory":
      ui.setMemoryQuery(args);
      ui.showPane("memory");
      return true;
    case "/terminal":
      ui.showPane("terminal");
      return true;
    default:
      return false;
  }
}
