import { useEffect, useRef } from "react";

import type { Command } from "../lib/protocol";

// The commands typing "/" offers, floating above the input. The Composer owns the
// selection and the keys.
export function SlashMenu({
  commands,
  selected,
  onHover,
  onPick,
}: {
  commands: Command[];
  selected: number;
  onHover: (index: number) => void;
  onPick: (command: Command) => void;
}) {
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    list.current?.children[selected]?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  return (
    <div
      ref={list}
      role="listbox"
      className="scroll-thin absolute bottom-full left-0 mb-2 max-h-64 w-full overflow-y-auto border border-border bg-popover py-1 text-[13px]"
    >
      {commands.map((command, i) => (
        <button
          key={command.cmd}
          role="option"
          aria-selected={i === selected}
          onMouseEnter={() => onHover(i)}
          onMouseDown={(event) => {
            // Keep the input focused
            event.preventDefault();
            onPick(command);
          }}
          className={`flex w-full gap-4 px-3 py-0.5 text-left ${
            i === selected ? "text-primary" : "text-muted-foreground"
          }`}
        >
          <span className="w-36 shrink-0">{command.cmd}</span>
          <span className="min-w-0 truncate text-dim">{command.desc}</span>
        </button>
      ))}
    </div>
  );
}

// The commands that complete what's typed, if it's a command name being typed.
export function matchingCommands(text: string, commands: Command[]): Command[] {
  if (!/^\/\S*$/.test(text)) return [];
  const typed = text.toLowerCase();
  return commands.filter((command) => command.cmd.startsWith(typed));
}
