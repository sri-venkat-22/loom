import { useEffect, useState } from "react";

import { type CheckpointDetail, api } from "../lib/api";
import { send } from "../lib/socket";
import { useSession } from "../store/session";
import { useUi } from "../store/ui";
import { ActionButton } from "./Buttons";

// Files listed in the dialog; the rest are counted
const MAX_FILES = 12;

const firstLine = (text: string) => text.trim().split("\n", 1)[0] || "(empty request)";

function FileList({ changes }: { changes: NonNullable<CheckpointDetail["changes"]> }) {
  const rows = [
    ...changes.changed.map((path) => ({ path, mark: "M", note: "", tone: "text-warning" })),
    ...changes.created.map((path) => ({
      path,
      mark: "−",
      note: "created since: deleted",
      tone: "text-destructive",
    })),
    ...changes.deleted.map((path) => ({
      path,
      mark: "+",
      note: "deleted since: restored",
      tone: "text-add",
    })),
  ];
  return (
    <div className="scroll-thin max-h-[220px] overflow-y-auto rounded-[10px] border border-border bg-background px-3 py-2 font-mono text-[12.5px] leading-[1.7]">
      {rows.slice(0, MAX_FILES).map((row) => (
        <div key={row.path} className="flex gap-2.5">
          <span className={`w-3 shrink-0 ${row.tone}`}>{row.mark}</span>
          <span className="min-w-0 truncate text-soft">{row.path}</span>
          {row.note && <span className="ml-auto shrink-0 text-dim">{row.note}</span>}
        </div>
      ))}
      {rows.length > MAX_FILES && (
        <div className="text-dim">… and {rows.length - MAX_FILES} more</div>
      )}
    </div>
  );
}

function summary(changes: NonNullable<CheckpointDetail["changes"]>) {
  const parts = [];
  const { changed, created, deleted } = changes;
  if (changed.length)
    parts.push(`${changed.length} file${changed.length === 1 ? "" : "s"} changed`);
  if (created.length) parts.push(`${created.length} created since`);
  if (deleted.length) parts.push(`${deleted.length} deleted since`);
  return parts.join(", ");
}

// Rewind to before a checkpoint: the code and the conversation, the code only or the
// conversation only, with what the files would lose. It runs loom's /rewind.
export function RewindDialog() {
  const id = useUi((state) => state.rewind);
  const close = () => useUi.getState().openRewind(null);
  const busy = useSession((state) => state.session?.busy ?? false);
  const item = useSession((state) => state.checkpoints?.items.find((cp) => cp.id === id));
  const [detail, setDetail] = useState<CheckpointDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!id) return;
    let live = true;
    setDetail(null);
    setError("");
    api
      .checkpoint(id)
      .then((found) => live && setDetail(found))
      .catch((err: Error) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, [id]);

  useEffect(() => {
    if (!id) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // Not the app's Esc, which would stop loom
        event.stopImmediatePropagation();
        close();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [id]);

  if (!id) return null;

  const prompt = item?.prompt ?? detail?.prompt ?? "";
  const conversation = item?.conversation ?? detail?.conversation ?? false;
  const changes = detail?.changes ?? null;
  const unchanged = changes && !summary(changes);
  const codeBlocked = !!detail?.busy || !!detail?.error;

  function rewind(what: "both" | "code" | "conversation") {
    if (!id) return;
    if (send({ type: "input", text: `/rewind ${id} ${what} --yes` })) {
      // The request goes back in the input, to change and send again
      if (what !== "code" && prompt.trim()) useUi.getState().fillInput(prompt);
      close();
    }
  }

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/50 px-4"
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Rewind"
        className="flex w-full max-w-[580px] flex-col gap-3.5 rounded-[16px] border border-border-strong bg-popover px-5 py-[18px] shadow-[0_16px_48px_rgb(0_0_0/0.5)]"
      >
        <div className="flex items-center gap-2.5">
          <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-primary">
            Rewind
          </span>
          {item?.time && (
            <span className="font-mono text-[12px] text-dim">{item.time.slice(11, 16)}</span>
          )}
        </div>
        <div className="text-[15px]">
          <span className="text-muted-foreground">Back to before </span>
          <span className="font-semibold">{firstLine(prompt)}</span>
        </div>

        {error && <div className="text-[13px] text-destructive">{error}</div>}
        {!detail && !error && <div className="text-[13px] text-dim">Comparing the files…</div>}
        {detail?.error && <div className="text-[13px] text-destructive">{detail.error}</div>}
        {changes && (
          <div className="flex flex-col gap-2">
            <div className="text-[13.5px] text-soft">
              {unchanged ? "The files are the same as then." : `Code: ${summary(changes)}`}
            </div>
            {!unchanged && <FileList changes={changes} />}
          </div>
        )}
        {detail && !detail.git && (
          <div className="text-[12.5px] text-dim">
            Without git, this covers the files the agent edited, not what commands changed.
          </div>
        )}
        {detail?.busy && <div className="text-[13px] text-warning">{detail.busy}</div>}
        {busy && (
          <div className="text-[13px] text-warning">loom is working: stop it first (Esc).</div>
        )}

        <div className="mt-1 flex flex-wrap items-center gap-2">
          {conversation && (
            <ActionButton
              variant="primary"
              onClick={() => rewind("both")}
              disabled={busy || codeBlocked}
            >
              Code and conversation
            </ActionButton>
          )}
          <ActionButton
            variant={conversation ? "secondary" : "primary"}
            onClick={() => rewind("code")}
            disabled={busy || codeBlocked || !!unchanged}
          >
            Code only
          </ActionButton>
          {conversation && (
            <ActionButton
              variant="secondary"
              onClick={() => rewind("conversation")}
              disabled={busy}
            >
              Conversation only
            </ActionButton>
          )}
          <ActionButton variant="ghost" hint="esc" onClick={close} className="ml-auto">
            Cancel
          </ActionButton>
        </div>
      </div>
    </div>
  );
}
