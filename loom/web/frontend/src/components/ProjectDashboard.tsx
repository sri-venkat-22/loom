import { useEffect, useMemo, useState } from "react";

import { type PhaseDiff, type ReportFormat, api } from "../lib/api";
import type { Decision, Metrics, PhaseTimeline, Timeline } from "../lib/protocol";
import { send } from "../lib/socket";
import { useSession } from "../store/session";
import { useUi } from "../store/ui";
import { Editor, FileChanges, Note } from "./PaneParts";

// The /project dashboard: the project's totals, then a timeline with a card per phase,
// from which its document, the diff of each run and going back to it are a click away.

export function formatCost(cost: number) {
  if (!cost) return "$0.00";
  return cost >= 0.01 ? `$${cost.toFixed(2)}` : `$${cost.toFixed(4)}`;
}

export function formatDuration(total: number) {
  const seconds = Math.round(total || 0);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

const plural = (n: number, word: string, words?: string) =>
  `${n} ${n === 1 ? word : (words ?? `${word}s`)}`;

function describe(metrics: Metrics) {
  return `${plural(metrics.runs, "run")} · ${formatDuration(metrics.seconds)} · ${formatCost(metrics.cost)}`;
}

// A phase's status as a chip
const STATUS: Record<string, [string, string]> = {
  approved: ["approved", "bg-success/15 text-add"],
  review: ["waiting for review", "bg-primary/15 text-primary"],
  running: ["running", "bg-warning/15 text-warning"],
  stale: ["to redo", "bg-warning/15 text-warning"],
  pending: ["pending", "bg-selected text-dim"],
};

function StatusChip({ phase }: { phase: PhaseTimeline }) {
  const which = phase.status === "pending" && phase.stale ? "stale" : phase.status;
  const [label, style] = STATUS[which] ?? STATUS.pending;
  return (
    <span className={`shrink-0 rounded-full px-2 py-px text-[11px] font-medium ${style}`}>
      {label}
    </span>
  );
}

function Dot({ phase }: { phase: PhaseTimeline }) {
  if (phase.status === "approved") {
    return (
      <span className="flex size-[18px] items-center justify-center rounded-full bg-primary text-[10px] font-bold text-on-primary">
        ✓
      </span>
    );
  }
  if (phase.current) {
    return (
      <span className="flex size-[18px] items-center justify-center rounded-full border-2 border-primary bg-pane">
        <span className="size-1.5 rounded-full bg-primary" />
      </span>
    );
  }
  return <span className="size-[18px] rounded-full border-[1.5px] border-border-strong bg-pane" />;
}

function SmallButton({
  onClick,
  disabled = false,
  title,
  danger = false,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`rounded-md border border-border-strong px-2 py-0.5 text-[12px] disabled:opacity-40 ${
        danger
          ? "text-muted-foreground enabled:hover:border-destructive/60 enabled:hover:text-destructive"
          : "text-soft enabled:hover:bg-bubble"
      }`}
    >
      {children}
    </button>
  );
}

// The download menu for the report: Markdown, Word or PDF
const FORMATS: [ReportFormat, string][] = [
  ["md", "Markdown"],
  ["docx", "Word (.docx)"],
  ["pdf", "PDF"],
];

function ReportMenu() {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [working, setWorking] = useState(false);

  async function download(format: ReportFormat) {
    setOpen(false);
    setWorking(true);
    setNote(null);
    try {
      const name = await api.downloadReport(format);
      const fell = !name.endsWith(`.${format}`);
      setNote({
        text: fell
          ? `Downloaded ${name}: Word needs pandoc, which loom can't find.`
          : `Downloaded ${name}`,
        error: false,
      });
    } catch (err) {
      setNote({ text: (err as Error).message, error: true });
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="relative flex shrink-0 flex-col items-end gap-1">
      <button
        onClick={() => setOpen(!open)}
        disabled={working}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-lg border border-border-strong bg-raised px-2.5 py-1 text-[12.5px] text-soft hover:bg-bubble disabled:opacity-60"
      >
        {working ? "Writing the report…" : "Download report"}
        <span className="text-[9px] text-dim">▼</span>
      </button>
      {open && (
        <>
          <button
            aria-label="Close the menu"
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-10 cursor-default"
          />
          <div
            role="menu"
            className="absolute top-full right-0 z-20 mt-1 flex w-40 flex-col rounded-lg border border-border-strong bg-popover p-1 shadow-lg"
          >
            {FORMATS.map(([format, label]) => (
              <button
                key={format}
                role="menuitem"
                onClick={() => download(format)}
                className="rounded-md px-2.5 py-1.5 text-left text-[12.5px] hover:bg-popover-selected"
              >
                {label}
              </button>
            ))}
          </div>
        </>
      )}
      {note && (
        <div
          className={`max-w-56 text-right text-[11.5px] ${note.error ? "text-destructive" : "text-dim"}`}
        >
          {note.text}
        </div>
      )}
    </div>
  );
}

// Each phase's cost as a bar, or its time when the models have no prices
function Bars({ phases, byCost }: { phases: PhaseTimeline[]; byCost: boolean }) {
  const value = (phase: PhaseTimeline) => (byCost ? phase.metrics.cost : phase.metrics.seconds);
  const most = Math.max(...phases.map(value), 0);
  if (!most) return null;
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[11px] text-dim">{byCost ? "Cost per phase" : "Time per phase"}</div>
      {phases.map((phase) => (
        <div
          key={phase.key}
          className="grid grid-cols-[64px_1fr_56px] items-center gap-2 text-[11.5px]"
        >
          <span className="truncate text-muted-foreground">{phase.key}</span>
          <span className="h-1.5 overflow-hidden rounded-full bg-selected">
            <span
              className="block h-full rounded-full bg-primary"
              style={{ width: `${(100 * value(phase)) / most}%` }}
            />
          </span>
          <span className="text-right font-mono text-[11px] text-muted-foreground">
            {byCost ? formatCost(phase.metrics.cost) : formatDuration(phase.metrics.seconds)}
          </span>
        </div>
      ))}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col rounded-lg bg-card px-2.5 py-1.5">
      <span className="truncate text-[15px] font-semibold">{value}</span>
      <span className="text-[11px] text-dim">{label}</span>
    </div>
  );
}

function Summary({ timeline }: { timeline: Timeline }) {
  const totals = timeline.totals;
  const idea = (timeline.idea ?? "").split("\n")[0];
  const byCost = Boolean(totals?.cost);
  return (
    <div className="flex flex-col gap-3 border-b border-raised px-3.5 py-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold uppercase tracking-[0.07em] text-primary">
            {timeline.complete ? "Project complete" : "Project"}
          </div>
          <div className="line-clamp-2 text-[14px] font-semibold leading-snug" title={idea}>
            {idea}
          </div>
          {(timeline.template || timeline.tdd) && (
            <div className="mt-0.5 text-[12px] text-dim">
              {[
                timeline.template && `from the ${timeline.template.name} template`,
                timeline.tdd && "test-driven Building",
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          )}
        </div>
        <ReportMenu />
      </div>
      {totals && (
        <div className="grid grid-cols-4 gap-1.5">
          <Stat label="time" value={formatDuration(totals.seconds)} />
          <Stat label="cost" value={formatCost(totals.cost)} />
          <Stat label={totals.runs === 1 ? "run" : "runs"} value={`${totals.runs}`} />
          <Stat label={totals.commits === 1 ? "commit" : "commits"} value={`${totals.commits}`} />
        </div>
      )}
      <Bars phases={timeline.phases} byCost={byCost} />
      <Decisions decisions={timeline.decisions} label="project" />
    </div>
  );
}

// The template's checks after a phase, passed or failed
function Checks({ checks }: { checks: PhaseTimeline["checks"] }) {
  if (!checks.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {checks.map((check) => (
        <span
          key={check.command}
          title={check.passed ? "The check passed" : "The check failed"}
          className={`max-w-full truncate rounded-md px-1.5 py-px font-mono text-[11px] ${
            check.passed ? "bg-success/12 text-add" : "bg-destructive/12 text-del"
          }`}
        >
          {check.passed ? "✓" : "✗"} {check.command}
        </span>
      ))}
    </div>
  );
}

function Decisions({ decisions, label = "" }: { decisions: Decision[]; label?: string }) {
  const [open, setOpen] = useState(false);
  if (!decisions.length) return null;
  return (
    <div className="flex flex-col gap-1">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-fit items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground"
      >
        <span className="w-2.5 text-[9px] text-dim">{open ? "▼" : "▶"}</span>
        {plural(decisions.length, label ? `${label} decision` : "decision")}
      </button>
      {open && (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0 pl-4">
          {decisions.map((decision) => (
            <li key={decision.id} className="text-[12.5px] leading-[1.5] text-soft">
              <span className="mr-1.5 font-mono text-[11px] text-primary">{decision.kind}</span>
              {decision.text}
              <span className="text-dim"> · {decision.source}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Going back to a phase asks first: it and the phases after it run again
function GoBack({ phase }: { phase: PhaseTimeline }) {
  const [asking, setAsking] = useState(false);
  const busy = useSession((state) => state.session?.busy ?? false);
  if (asking) {
    return (
      <span className="flex flex-wrap items-center gap-1.5 text-[12px] text-soft">
        Go back to {phase.title}? It and the later phases run again.
        <SmallButton
          danger
          disabled={busy}
          onClick={() => {
            setAsking(false);
            send({ type: "input", text: `/project back ${phase.key}` });
          }}
        >
          Go back
        </SmallButton>
        <SmallButton onClick={() => setAsking(false)}>Cancel</SmallButton>
      </span>
    );
  }
  return (
    <SmallButton
      danger
      disabled={busy}
      title={busy ? "loom is busy" : `Redo ${phase.title} and the phases after it`}
      onClick={() => setAsking(true)}
    >
      Go back here
    </SmallButton>
  );
}

// Test-driven Building: the acceptance tests and the latest build's tries
function TestDriven({ phase }: { phase: PhaseTimeline }) {
  const { spec } = phase;
  if (!spec) return null;
  const build = [...phase.run_log].reverse().find((run) => run.step === "build");
  const attempts = build?.attempts ?? [];
  const label =
    spec.status === "approved"
      ? `${plural(spec.tests.length, "acceptance test file")} locked`
      : spec.status === "review"
        ? "acceptance tests waiting for review"
        : "acceptance tests to write";
  return (
    <div className="flex flex-col gap-1 text-[12px]">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground">Test-driven · {label}</span>
        {attempts.map((attempt) => (
          <span
            key={attempt.attempt}
            title={`Attempt ${attempt.attempt}: ${
              attempt.passed
                ? "the tests pass"
                : attempt.passed === false
                  ? "tests fail"
                  : "not run"
            } · ${formatDuration(attempt.seconds)} · ${formatCost(attempt.cost)} so far`}
            className={`rounded px-1 font-mono text-[11px] ${
              attempt.passed ? "bg-success/12 text-add" : "bg-destructive/12 text-del"
            }`}
          >
            {attempt.passed ? "✓" : "✗"}
          </span>
        ))}
        {build?.result && (
          <span className={build.result === "passed" ? "text-add" : "text-warning"}>
            {RESULTS[build.result] ?? build.result}
          </span>
        )}
      </div>
      {(build?.skips ?? []).length > 0 && (
        <div className="text-warning">
          {build!.skips!.length === 1
            ? "1 test change skips a test or expects it to fail"
            : `${build!.skips!.length} test changes skip tests or expect them to fail`}
        </div>
      )}
    </div>
  );
}

// Parallel Building's work packages, by wave
const PACKAGE_STYLE: Record<string, string> = {
  merged: "bg-success/12 text-add",
  built: "bg-primary/12 text-primary",
  running: "bg-warning/15 text-warning",
  failed: "bg-destructive/12 text-del",
  stopped: "bg-destructive/12 text-del",
  pending: "bg-selected text-dim",
};

function Packages({ phase }: { phase: PhaseTimeline }) {
  if (!phase.packages.length) return null;
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[12px] text-muted-foreground">
        Parallel builders · {plural(phase.packages.length, "work package")}
      </div>
      {phase.packages.map((pkg) => (
        <div key={pkg.id} className="flex min-w-0 items-center gap-2 text-[12px]">
          <span className="w-4 shrink-0 text-right font-mono text-[11px] text-dim">
            {pkg.wave ?? ""}
          </span>
          <span className="min-w-0 truncate font-mono text-soft" title={pkg.title}>
            {pkg.id}
          </span>
          <span
            className={`shrink-0 rounded px-1.5 py-px text-[11px] ${PACKAGE_STYLE[pkg.status] ?? PACKAGE_STYLE.pending}`}
            title={pkg.error ?? undefined}
          >
            {pkg.status}
          </span>
          <span className="ml-auto shrink-0 font-mono text-[11px] text-dim">
            {pkg.attempts ? `${plural(pkg.attempts, "try", "tries")} · ` : ""}
            {formatCost(pkg.cost)}
          </span>
        </div>
      ))}
    </div>
  );
}

const RESULTS: Record<string, string> = {
  passed: "tests pass",
  failed: "tests still fail",
  budget: "budget ran out",
  not_run: "tests not run",
  stopped: "stopped",
};

type View = { kind: "document" | "diff"; phase: PhaseTimeline; path?: string };

function PhaseCard({
  phase,
  last,
  canGoBack,
  focused,
  show,
}: {
  phase: PhaseTimeline;
  last: boolean;
  canGoBack: boolean;
  focused: boolean;
  show: (view: View) => void;
}) {
  const { metrics } = phase;
  const ran = metrics.runs > 0;
  const committed = phase.run_log.some((run) => run.commits > 0 && run.head);
  const details = [phase.produces];
  if (ran) details.push(describe(metrics));
  if (phase.fix_rounds) details.push(plural(phase.fix_rounds, "fix round"));
  const lastRun = phase.run_log[phase.run_log.length - 1];

  return (
    <li id={`phase-${phase.key}`} className="relative pb-3 pl-7">
      {!last && <span className="absolute top-5 bottom-0 left-[8.5px] w-px bg-border-strong" />}
      <span className="absolute top-2.5 left-0">
        <Dot phase={phase} />
      </span>
      <div
        className={`flex flex-col gap-1.5 rounded-[10px] border bg-card px-3 py-2.5 ${
          focused ? "border-primary/70" : phase.current ? "border-primary/35" : "border-selected"
        }`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="font-mono text-[11px] text-dim">{phase.number}</span>
          <span className="truncate text-[13.5px] font-semibold">{phase.title}</span>
          <StatusChip phase={phase} />
          {phase.verdict && (
            <span className="ml-auto shrink-0 rounded-full bg-selected px-2 py-px font-mono text-[11px] font-semibold text-soft">
              {phase.verdict}
            </span>
          )}
        </div>
        <div className="text-[12px] text-dim">
          {details.join(" · ")}
          {!ran && " · not run yet"}
          {lastRun && lastRun.outcome !== "done" && (
            <span className="text-warning"> · last run {lastRun.outcome}</span>
          )}
        </div>
        <TestDriven phase={phase} />
        <Packages phase={phase} />
        <Checks checks={phase.checks} />
        <Decisions decisions={phase.decisions} />
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          <SmallButton
            disabled={!ran && phase.status === "pending"}
            title={`Read ${phase.document}`}
            onClick={() => show({ kind: "document", phase })}
          >
            Document
          </SmallButton>
          {phase.spec && phase.spec.status !== "pending" && (
            <SmallButton
              title={`Read ${phase.spec.document}`}
              onClick={() => show({ kind: "document", phase, path: phase.spec!.document })}
            >
              Tests
            </SmallButton>
          )}
          <SmallButton
            disabled={!committed}
            title={committed ? "What its runs changed" : "Its runs made no commits"}
            onClick={() => show({ kind: "diff", phase })}
          >
            Diff
          </SmallButton>
          {canGoBack && <GoBack phase={phase} />}
        </div>
      </div>
    </li>
  );
}

function BackBar({ onBack, children }: { onBack: () => void; children: React.ReactNode }) {
  return (
    <div className="flex shrink-0 items-center gap-2.5 border-b border-raised px-3.5 py-2">
      <button
        onClick={onBack}
        className="shrink-0 rounded-md px-1.5 py-0.5 text-[12.5px] text-muted-foreground hover:bg-raised hover:text-foreground"
      >
        ← Timeline
      </button>
      {children}
    </div>
  );
}

function DocumentView({ path, onBack }: { path: string; onBack: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const busy = useSession((state) => state.session?.busy ?? false);

  useEffect(() => {
    if (busy) return;
    let live = true;
    api
      .file(path)
      .then((file) => live && (setText(file.text), setError("")))
      .catch((err: Error) => live && (setText(null), setError(err.message)));
    return () => {
      live = false;
    };
  }, [path, busy]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <BackBar onBack={onBack}>
        <span className="min-w-0 truncate font-mono text-[12px] text-soft">{path}</span>
        <span className="ml-auto shrink-0 text-[11px] text-dim">read-only</span>
      </BackBar>
      <div className="min-h-0 flex-1">
        {error && (
          <div className="p-3.5">
            <Note error>{error}</Note>
          </div>
        )}
        {text !== null && <Editor path={path} value={text} readOnly />}
      </div>
    </div>
  );
}

function RunDiffView({ phase, onBack }: { phase: PhaseTimeline; onBack: () => void }) {
  const [run, setRun] = useState<number | undefined>(undefined);
  const [diff, setDiff] = useState<PhaseDiff | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api
      .phaseDiff(phase.key, run)
      .then((found) => live && (setDiff(found), setError("")))
      .catch((err: Error) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, [phase.key, run]);

  const runs = diff?.runs.filter((entry) => entry.commits > 0) ?? [];
  return (
    <div className="flex h-full min-h-0 flex-col">
      <BackBar onBack={onBack}>
        <span className="min-w-0 truncate text-[12.5px] font-semibold">{phase.title}</span>
        {runs.length > 0 && (
          <select
            aria-label="Run"
            value={diff?.run ?? ""}
            onChange={(event) => setRun(Number(event.target.value))}
            className="ml-auto rounded-md border border-border-strong bg-raised px-1.5 py-0.5 text-[12px] text-soft"
          >
            {runs.map((entry) => (
              <option key={entry.run} value={entry.run}>
                Run {entry.run} · {plural(entry.commits, "commit")} · {entry.outcome}
              </option>
            ))}
          </select>
        )}
      </BackBar>
      <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto p-3">
        {error && <Note error>{error}</Note>}
        {diff && !diff.available && <Note>No runs of {phase.title} to show.</Note>}
        {diff?.available && (
          <div className="font-mono text-[11.5px] text-dim">
            {(diff.base ?? "start").slice(0, 7)}..{(diff.head ?? "").slice(0, 7)}
          </div>
        )}
        {diff?.available && !diff.files.length && <Note>This run changed no files.</Note>}
        {diff && <FileChanges files={diff.files} />}
      </div>
    </div>
  );
}

export function ProjectTab() {
  const timeline = useSession((state) => state.timeline);
  const focus = useUi((state) => state.projectFocus);
  const [view, setView] = useState<View | null>(null);

  // Opening a phase from the breadcrumb shows the timeline at its card
  useEffect(() => {
    if (!focus.n) return;
    setView(null);
    const timer = setTimeout(() => {
      if (focus.key) {
        document.getElementById(`phase-${focus.key}`)?.scrollIntoView({ block: "nearest" });
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [focus]);

  const currentNumber = useMemo(() => {
    const current = timeline?.phases.find((phase) => phase.current);
    return current ? current.number : Infinity;
  }, [timeline]);

  if (!timeline) {
    return (
      <div className="p-3.5">
        <Note>Loading the project…</Note>
      </div>
    );
  }
  if (!timeline.available) {
    return (
      <div className="p-3.5">
        <Note>No project yet. Start one with /project new IDEA.</Note>
      </div>
    );
  }

  // A phase's card may have changed since it was opened
  const latest = (phase: PhaseTimeline) =>
    timeline.phases.find((found) => found.key === phase.key) ?? phase;
  if (view?.kind === "document") {
    return (
      <DocumentView path={view.path ?? latest(view.phase).document} onBack={() => setView(null)} />
    );
  }
  if (view?.kind === "diff") {
    return <RunDiffView phase={latest(view.phase)} onBack={() => setView(null)} />;
  }

  return (
    <div className="scroll-thin h-full overflow-y-auto">
      <Summary timeline={timeline} />
      <ol className="m-0 list-none px-3.5 pt-3.5 pb-1">
        {timeline.phases.map((phase, i) => (
          <PhaseCard
            key={phase.key}
            phase={phase}
            last={i === timeline.phases.length - 1}
            // Back to an earlier phase, or to the current one once it has run
            canGoBack={
              phase.number < currentNumber ||
              (phase.number === currentNumber && phase.status !== "pending")
            }
            focused={focus.key === phase.key}
            show={setView}
          />
        ))}
      </ol>
    </div>
  );
}
