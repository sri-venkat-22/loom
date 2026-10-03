import { Fragment } from "react";

import { send } from "../lib/socket";
import type { Phase } from "../lib/protocol";
import { useSession } from "../store/session";

// A stable empty list: a selector returning a new one each time would render forever
const NO_PHASES: Phase[] = [];

function Dot({ state }: { state: "done" | "current" | "future" }) {
  if (state === "done") {
    return (
      <span className="flex size-[18px] items-center justify-center rounded-full bg-primary/20 text-[10px] font-bold text-primary">
        ✓
      </span>
    );
  }
  if (state === "current") {
    return (
      <span className="flex size-3.5 items-center justify-center rounded-full border-2 border-primary">
        <span className="size-1.5 rounded-full bg-primary" />
      </span>
    );
  }
  return <span className="size-3.5 rounded-full border-[1.5px] border-border-strong" />;
}

// idea — planning — design — building — testing — launch, as steps: approved ones ticked,
// the current one ringed. Clicking a phase goes back to it, after loom asks.
export function PhaseBreadcrumb() {
  const phases = useSession((state) => state.session?.phases ?? NO_PHASES);
  const current = useSession((state) => state.session?.phase ?? null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const project = useSession((state) => state.session?.project ?? null);

  if (!phases.length) return <div className="flex-1" />;
  const currentIndex = phases.findIndex((phase) => phase.key === current);

  return (
    <nav
      // Centered, but scrolling from the first phase when they don't fit
      style={{ justifyContent: "safe center" }}
      className="no-scrollbar flex min-w-0 flex-1 items-center overflow-x-auto"
      title={project ? `/project: ${project}` : "No project yet: start one with /project new IDEA"}
    >
      {phases.map((phase, i) => {
        const isCurrent = phase.key === current;
        const done = phase.status === "approved" || (currentIndex >= 0 && i < currentIndex);
        const state = isCurrent ? "current" : done ? "done" : "future";
        return (
          <Fragment key={phase.key}>
            {i > 0 && <span className="h-[1.5px] w-3.5 shrink-0 rounded-sm bg-border-strong" />}
            <button
              disabled={busy || !project}
              onClick={() => send({ type: "input", text: `/phase ${phase.key}` })}
              title={`${phase.title}: ${phase.status}`}
              className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full py-1 pl-1 pr-2 text-[13px] enabled:hover:bg-raised"
            >
              <Dot state={state} />
              <span
                className={
                  isCurrent
                    ? "font-semibold text-foreground"
                    : done
                      ? "text-muted-foreground"
                      : "text-dim"
                }
              >
                {phase.key}
              </span>
            </button>
          </Fragment>
        );
      })}
    </nav>
  );
}
