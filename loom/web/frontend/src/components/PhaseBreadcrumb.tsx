import { Fragment } from "react";

import { send } from "../lib/socket";
import type { Phase } from "../lib/protocol";
import { useSession } from "../store/session";

// A stable empty list: a selector returning a new one each time would render forever
const NO_PHASES: Phase[] = [];

// idea › planning › design › building › testing › launch, with the project's current
// phase in bold. Clicking a phase goes back to it, after loom asks.
export function PhaseBreadcrumb() {
  const phases = useSession((state) => state.session?.phases ?? NO_PHASES);
  const current = useSession((state) => state.session?.phase ?? null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const project = useSession((state) => state.session?.project ?? null);

  if (!phases.length) return null;
  const currentIndex = phases.findIndex((phase) => phase.key === current);

  return (
    <nav
      className="flex min-w-0 items-center gap-4 overflow-hidden text-dim"
      title={project ?? "No project yet: start one with /project new IDEA"}
    >
      {phases.map((phase, i) => {
        const isCurrent = phase.key === current;
        const done = phase.status === "approved" || (currentIndex >= 0 && i < currentIndex);
        return (
          <Fragment key={phase.key}>
            {i > 0 && <span className="text-border">›</span>}
            <button
              disabled={busy || !project}
              onClick={() => send({ type: "input", text: `/phase ${phase.key}` })}
              title={`${phase.title}: ${phase.status}`}
              className={
                isCurrent
                  ? "font-bold text-primary"
                  : done
                    ? "text-muted-foreground enabled:hover:text-foreground"
                    : "enabled:hover:text-foreground"
              }
            >
              {phase.key}
            </button>
          </Fragment>
        );
      })}
    </nav>
  );
}
