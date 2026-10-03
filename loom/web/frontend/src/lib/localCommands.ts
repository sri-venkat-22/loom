import { useUi } from "../store/ui";
import type { Command } from "./protocol";
import { applyTheme, currentTheme } from "./theme";

// Commands the browser handles itself, without loom: they open parts of the UI.
export const LOCAL_COMMANDS: Command[] = [
  {
    cmd: "/dashboard",
    desc: "Open the project dashboard: each phase's runs, cost, decisions, document and diffs",
  },
  { cmd: "/files", desc: "Open the file tree, to add files to the chat or view them (⌘B)" },
  { cmd: "/memory", desc: "Search the project's shared memory: /memory [QUERY]" },
  { cmd: "/terminal", desc: "Show the output of /run" },
  { cmd: "/theme", desc: "Switch between the dark and light themes: /theme [dark|light]" },
];

// Run text if it's one of LOCAL_COMMANDS. Returns whether it was.
export function runLocal(text: string): boolean {
  const [name, ...rest] = text.trim().split(/\s+/);
  const args = rest.join(" ");
  const ui = useUi.getState();
  switch (name) {
    case "/dashboard":
      ui.openProject();
      return true;
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
    case "/theme":
      if (args === "dark" || args === "light") applyTheme(args);
      else applyTheme(currentTheme() === "dark" ? "light" : "dark");
      return true;
    default:
      return false;
  }
}
