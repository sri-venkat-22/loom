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
    <div className="absolute inset-x-6 bottom-full z-10 mb-2 rounded-[14px] border border-border-strong bg-popover p-1.5 shadow-[0_12px_32px_rgb(0_0_0/0.45)]">
      <div ref={list} role="listbox" className="scroll-thin max-h-[260px] overflow-y-auto">
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
            className={`grid w-full grid-cols-[130px_minmax(0,1fr)] gap-3 rounded-lg px-2.5 py-[7px] text-left ${
              i === selected ? "bg-popover-selected" : ""
            }`}
          >
            <span
              className={`font-mono text-[13px] ${i === selected ? "text-primary" : "text-soft"}`}
            >
              {command.cmd}
            </span>
            <span className={`truncate text-[13px] ${i === selected ? "text-subtle" : "text-dim"}`}>
              {command.desc}
            </span>
          </button>
        ))}
      </div>
      <div className="mt-1 flex gap-3.5 border-t border-border-strong px-2.5 pb-1 pt-2 text-[11.5px] text-dim">
        <span>↑↓ choose</span>
        <span>⇥ complete</span>
        <span>↵ run</span>
        <span>esc close</span>
      </div>
    </div>
  );
}

// The commands that complete what's typed, if it's a command name being typed.
export function matchingCommands(text: string, commands: Command[]): Command[] {
  if (!/^\/\S*$/.test(text)) return [];
  const typed = text.toLowerCase();
  return commands.filter((command) => command.cmd.startsWith(typed));
}
