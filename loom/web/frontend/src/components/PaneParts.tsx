import { Suspense, lazy, useState } from "react";

import type { FileChange } from "../lib/api";
import { useUi } from "../store/ui";
import { DiffView } from "./DiffView";

// Pieces the side pane's tabs share

const MonacoView = lazy(() => import("./MonacoView"));

// Monaco, loaded the first time a tab shows a file
export function Editor(props: React.ComponentProps<typeof MonacoView>) {
  return (
    <Suspense fallback={<div className="p-4 text-dim">Loading the editor…</div>}>
      <MonacoView {...props} />
    </Suspense>
  );
}

export function Note({ children, error = false }: { children: React.ReactNode; error?: boolean }) {
  return <div className={`text-[13px] ${error ? "text-destructive" : "text-dim"}`}>{children}</div>;
}

const STATUS_LABEL: Record<FileChange["status"], string> = {
  added: "new",
  modified: "",
  deleted: "deleted",
  renamed: "renamed",
};

// A stable empty list, for selectors and defaults
export const NO_FILES: FileChange[] = [];

// Changed files, each a card with its diff that folds
export function FileChanges({ files }: { files: FileChange[] }) {
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const openInViewer = useUi((state) => state.openInViewer);

  function fold(path: string) {
    setFolded((before) => {
      const next = new Set(before);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  return (
    <>
      {files.map((file) => {
        const open = !folded.has(file.path);
        return (
          <div
            key={file.path}
            className="overflow-hidden rounded-[10px] border border-selected bg-background"
          >
            <div className="flex items-center gap-2 px-2.5 py-2">
              <button
                onClick={() => fold(file.path)}
                aria-label={open ? "Fold" : "Unfold"}
                className="w-[18px] shrink-0 text-[10px] text-dim"
              >
                {open ? "▼" : "▶"}
              </button>
              <button
                onClick={() => file.status !== "deleted" && openInViewer(file.path)}
                title={file.status === "deleted" ? file.path : `View ${file.path}`}
                className="min-w-0 truncate text-left font-mono text-[12.5px] text-foreground hover:underline"
              >
                {file.path}
              </button>
              {STATUS_LABEL[file.status] && (
                <span className="shrink-0 rounded-full bg-selected px-[7px] py-px text-[11px] text-muted-foreground">
                  {STATUS_LABEL[file.status]}
                  {file.old_path && ` from ${file.old_path}`}
                </span>
              )}
              <span className="ml-auto shrink-0 font-mono text-[12px]">
                <span className="text-add">+{file.added}</span>{" "}
                <span className="text-del">-{file.removed}</span>
              </span>
            </div>
            {open &&
              (file.binary ? (
                <div className="border-t border-raised px-3 py-2 text-[12px] text-dim">
                  Binary file
                </div>
              ) : (
                file.lines.length > 0 && (
                  <DiffView
                    inCard
                    diff={{
                      type: "diff",
                      id: file.path,
                      tool_id: null,
                      file: file.path,
                      lines: file.lines,
                    }}
                  />
                )
              ))}
          </div>
        );
      })}
    </>
  );
}
