import { Suspense, lazy, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { type FileChange, type Files, type Memory, api } from "../lib/api";
import { answer } from "../lib/asks";
import { changeTotals, useChanges } from "../lib/changes";
import { send } from "../lib/socket";
import { pendingAsk, useSession } from "../store/session";
import { type Pane, useUi } from "../store/ui";
import { DiffView } from "./DiffView";

const MonacoView = lazy(() => import("./MonacoView"));

const TABS: [Pane, string][] = [
  ["changes", "Changes"],
  ["files", "Files"],
  ["terminal", "Terminal"],
  ["memory", "Memory"],
];

// Commands from the pane run only when loom is ready for one
function useIdle() {
  const busy = useSession((state) => state.session?.busy ?? true);
  const open = useSession((state) => state.connection === "open");
  return open && !busy;
}

function quote(path: string) {
  return /\s/.test(path) ? `"${path}"` : path;
}

function Editor(props: React.ComponentProps<typeof MonacoView>) {
  return (
    <Suspense fallback={<div className="p-4 text-dim">Loading the editor…</div>}>
      <MonacoView {...props} />
    </Suspense>
  );
}

function Note({ children, error = false }: { children: React.ReactNode; error?: boolean }) {
  return <div className={`text-[13px] ${error ? "text-destructive" : "text-dim"}`}>{children}</div>;
}

// Changes

const STATUS_LABEL: Record<FileChange["status"], string> = {
  added: "new",
  modified: "",
  deleted: "deleted",
  renamed: "renamed",
};

const BASES = [
  ["session", "Since loom started"],
  ["branch", "Since main"],
] as const;

function ChangesTab() {
  const [base, setBase] = useState<"session" | "branch">("session");
  const { changes, error } = useChanges(base);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const openInViewer = useUi((state) => state.openInViewer);

  const files = changes?.files ?? [];
  const { added, removed } = changeTotals(changes);

  function fold(path: string) {
    setFolded((before) => {
      const next = new Set(before);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-2.5 border-b border-raised px-3.5 py-2.5">
        <div className="flex rounded-full border border-border bg-card p-[3px]">
          {BASES.map(([which, label]) => (
            <button
              key={which}
              onClick={() => setBase(which)}
              className={`whitespace-nowrap rounded-full px-3 py-1 text-[12px] ${
                which === base
                  ? "bg-border-strong text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {files.length > 0 && (
          <span className="ml-auto whitespace-nowrap text-[12px] text-muted-foreground">
            {files.length} file{files.length === 1 ? "" : "s"}{" "}
            <span className="font-mono">
              <span className="text-add">+{added}</span>{" "}
              <span className="text-del">-{removed}</span>
            </span>
          </span>
        )}
      </div>
      <div className="scroll-thin flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto p-3">
        {error && <Note error>{error}</Note>}
        {changes && !changes.available && (
          <Note>
            {base === "branch"
              ? "There's no main or master branch to compare with."
              : "Changes show here in a git repo."}
          </Note>
        )}
        {changes?.available && !files.length && <Note>No changes {changes.label}.</Note>}
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
      </div>
    </div>
  );
}

// Files

interface TreeNode {
  name: string;
  path: string;
  dir: boolean;
  children: TreeNode[];
}

function buildTree(paths: string[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", dir: true, children: [] };
  for (const path of paths) {
    let node = root;
    const parts = path.split("/");
    parts.forEach((part, i) => {
      const file = i === parts.length - 1;
      const sub = parts.slice(0, i + 1).join("/");
      let child = node.children.find((c) => c.name === part && c.dir === !file);
      if (!child) {
        child = { name: part, path: sub, dir: !file, children: [] };
        node.children.push(child);
      }
      node = child;
    });
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name));
    nodes.forEach((n) => sort(n.children));
  };
  sort(root.children);
  return root.children;
}

function FilesTab() {
  const [files, setFiles] = useState<Files | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const chatFiles = useSession((state) => state.session?.files);
  const idle = useIdle();
  const openFile = useUi((state) => state.openFile);
  const openInViewer = useUi((state) => state.openInViewer);

  // Read the list again when the files in the chat change
  useEffect(() => {
    api
      .files()
      .then((found) => {
        setFiles(found);
        setError("");
        // Open the folders of the files in the chat, and of the one in the viewer
        setExpanded((before) => {
          const next = new Set(before);
          for (const path of [...found.chat, ...(openFile ? [openFile] : [])]) {
            const parts = path.split("/");
            for (let i = 1; i < parts.length; i++) next.add(parts.slice(0, i).join("/"));
          }
          return next;
        });
      })
      .catch((err: Error) => setError(err.message));
  }, [chatFiles, openFile]);

  const tree = useMemo(() => buildTree(files?.files ?? []), [files]);
  const inChat = useMemo(() => new Set(files?.chat ?? []), [files]);
  const readOnly = useMemo(() => new Set(files?.read_only ?? []), [files]);
  const chatCount = inChat.size + readOnly.size;

  function toggle(path: string) {
    if (!idle) return;
    const command = inChat.has(path) || readOnly.has(path) ? "/drop" : "/add";
    send({ type: "input", text: `${command} ${quote(path)}` });
  }

  function rows(nodes: TreeNode[], depth: number): React.ReactNode[] {
    return nodes.flatMap((node) => {
      const pad = { paddingLeft: 8 + depth * 18 };
      if (node.dir) {
        const open = expanded.has(node.path);
        return [
          <button
            key={node.path}
            style={pad}
            onClick={() =>
              setExpanded((before) => {
                const next = new Set(before);
                if (open) next.delete(node.path);
                else next.add(node.path);
                return next;
              })
            }
            className="flex h-7 w-full items-center gap-2 rounded-md pr-2 text-left font-mono text-[12.5px] text-muted-foreground hover:bg-card hover:text-foreground"
          >
            <span className="w-3.5 shrink-0 text-[9px]">{open ? "▼" : "▶"}</span>
            {node.name}/
          </button>,
          ...(open ? rows(node.children, depth + 1) : []),
        ];
      }
      const included = inChat.has(node.path);
      const ro = readOnly.has(node.path);
      const viewing = openFile === node.path;
      return [
        <div
          key={node.path}
          style={pad}
          className={`flex h-7 items-center gap-2 rounded-md pr-2 ${viewing ? "bg-popover" : ""}`}
        >
          <button
            onClick={() => toggle(node.path)}
            disabled={!idle}
            title={
              included
                ? "In the chat · click to /drop"
                : ro
                  ? "In the chat, read-only · click to /drop"
                  : "Add to the chat (/add)"
            }
            aria-label={included || ro ? `Drop ${node.path}` : `Add ${node.path}`}
            className={`flex size-[15px] shrink-0 items-center justify-center rounded p-0 text-[10px] font-bold leading-none ${
              included || ro
                ? "bg-primary text-on-primary"
                : "border-[1.5px] border-faint enabled:hover:border-muted-foreground"
            }`}
          >
            {included ? "✓" : ro ? "r" : ""}
          </button>
          <button
            onClick={() => openInViewer(node.path)}
            className={`min-w-0 flex-1 truncate text-left font-mono text-[12.5px] ${
              viewing ? "text-foreground" : "text-subtle hover:text-foreground"
            }`}
          >
            {node.name}
          </button>
        </div>,
      ];
    });
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-2">
        {error && <Note error>{error}</Note>}
        {files && !files.files.length && <Note>No files.</Note>}
        {rows(tree, 0)}
        {files && (
          <div className="px-2 pt-2.5 text-[12px] text-dim">
            {chatCount} file{chatCount === 1 ? "" : "s"} in the chat
          </div>
        )}
      </div>
      {openFile && <FileViewer path={openFile} />}
    </div>
  );
}

function FileViewer({ path }: { path: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState("");
  const busy = useSession((state) => state.session?.busy ?? false);

  // Read it again after each request, which may have changed it
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
    <div className="flex h-[55%] min-h-0 shrink-0 flex-col border-t border-line">
      <div className="flex items-center gap-2.5 border-b border-raised px-3.5 py-2">
        <span className="min-w-0 truncate font-mono text-[12px] text-soft">{path}</span>
        <span className="shrink-0 text-[11px] text-dim">read-only</span>
        <button
          onClick={() => useUi.setState({ openFile: null })}
          aria-label="Close the file"
          className="ml-auto text-[15px] text-dim hover:text-foreground"
        >
          ×
        </button>
      </div>
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

// Memory

function MemoryTab() {
  const query = useUi((state) => state.memoryQuery);
  const setQuery = useUi((state) => state.setMemoryQuery);
  const [memory, setMemory] = useState<Memory | null>(null);
  const [error, setError] = useState("");
  const [searching, setSearching] = useState(false);
  const busy = useSession((state) => state.session?.busy ?? false);

  useEffect(() => {
    let live = true;
    const timer = setTimeout(() => {
      setSearching(true);
      api
        .memory(query)
        .then((found) => live && (setMemory(found), setError("")))
        .catch((err: Error) => live && setError(err.message))
        .finally(() => live && setSearching(false));
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [query, busy]);

  const card = "flex flex-col gap-1 rounded-[10px] border border-selected px-3 py-2.5";

  return (
    <div className="scroll-thin flex h-full flex-col gap-2.5 overflow-y-auto p-3.5">
      <input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search project memory…"
        className="rounded-full border border-border-strong bg-background px-3.5 py-2 text-[13px] outline-none placeholder:text-dim"
      />
      {error && <Note error>{error}</Note>}
      {memory && !memory.available && (
        <Note>No project memory yet. Start a project with /project new IDEA.</Note>
      )}
      {memory?.available && (
        <div className="text-[12px] text-dim">
          {searching
            ? "Searching…"
            : query.trim()
              ? `Matches${memory.store ? ` · ${memory.store}` : ""}`
              : "Decisions"}
        </div>
      )}
      {memory?.available && query.trim() && !memory.hits.length && !searching && (
        <Note>Nothing matches.</Note>
      )}
      {query.trim() &&
        memory?.hits.map((hit) => (
          <div key={hit.id} className={card}>
            <div className="flex gap-2.5 text-[11.5px] text-dim">
              <span className="truncate font-mono">{hit.source ?? hit.id}</span>
              <span className="text-primary">{hit.kind}</span>
              {hit.phase && <span className="ml-auto shrink-0">{hit.phase}</span>}
            </div>
            {hit.title && <div className="text-[13px] font-medium">{hit.title}</div>}
            <div className="line-clamp-6 whitespace-pre-wrap break-words text-[13px] leading-[1.55] text-soft">
              {hit.text}
            </div>
          </div>
        ))}
      {memory?.available && !query.trim() && !memory.decisions.length && (
        <Note>No decisions recorded yet.</Note>
      )}
      {!query.trim() &&
        memory?.decisions.map((decision) => (
          <div key={decision.id} className={card}>
            <div className="flex gap-2.5 text-[11.5px] text-dim">
              <span className="font-mono">d-{decision.id}</span>
              <span className="text-primary">{decision.kind}</span>
              <span className="truncate">{decision.source}</span>
              <span className="ml-auto shrink-0">{decision.phase}</span>
            </div>
            <div className="whitespace-pre-wrap break-words text-[13px] leading-[1.55] text-soft">
              {decision.text}
            </div>
            {decision.reason && <div className="text-[12px] text-dim">why: {decision.reason}</div>}
          </div>
        ))}
    </div>
  );
}

// Terminal

function TerminalTab() {
  const terminal = useSession((state) => state.terminal);
  const running = useSession((state) =>
    state.entries.some((e) => e.kind === "tool" && e.name === "Run" && e.status === "running"),
  );
  const scroller = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [terminal]);

  return (
    <div ref={scroller} className="scroll-thin h-full overflow-y-auto px-4 py-3.5">
      {!terminal && <Note>Output of /run shows here.</Note>}
      <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[12.5px] leading-[1.6] text-subtle">
        {terminal}
        {running && <span className="blink">▍</span>}
      </pre>
    </div>
  );
}

// Editing a /project document at a checkpoint

function useEditAsk() {
  const entries = useSession((state) => state.entries);
  const ask = pendingAsk(entries)?.ask;
  return ask?.kind === "edit" ? ask : undefined;
}

export function SidePane() {
  const pane = useUi((state) => state.pane);
  const showPane = useUi((state) => state.showPane);
  const editAsk = useEditAsk();
  const [draft, setDraft] = useState("");

  // A new document to edit starts from its text
  useEffect(() => {
    if (editAsk) setDraft(editAsk.default);
  }, [editAsk]);

  if (!pane && !editAsk) return null;

  return (
    <aside className="flex w-[clamp(380px,38vw,620px)] min-w-[280px] shrink flex-col border-l border-line bg-pane text-[13px]">
      <div className="flex h-[46px] shrink-0 items-center gap-1 border-b border-line px-2.5">
        {editAsk ? (
          <>
            <span className="min-w-0 truncate px-2.5 py-[5px] text-[13px] text-primary">
              Editing <span className="font-mono text-[12px]">{editAsk.subject}</span>
            </span>
            <span className="flex-1" />
            <button
              onClick={() => answer(editAsk, editAsk.default)}
              className="rounded-lg px-3 py-[5px] text-[13px] text-muted-foreground hover:bg-raised hover:text-foreground"
            >
              Cancel
            </button>
            <button
              onClick={() => answer(editAsk, draft)}
              className="flex items-center gap-2 rounded-lg bg-primary px-3 py-[5px] text-[13px] font-semibold text-on-primary hover:bg-primary-hover"
            >
              Save <span className="font-mono text-[11px] opacity-70">⌘S</span>
            </button>
          </>
        ) : (
          <>
            {TABS.map(([tab, label]) => (
              <button
                key={tab}
                onClick={() => showPane(tab)}
                className={`whitespace-nowrap rounded-lg px-[11px] py-[5px] text-[13px] ${
                  tab === pane
                    ? "bg-selected text-foreground"
                    : "text-muted-foreground hover:bg-card hover:text-foreground"
                }`}
              >
                {label}
              </button>
            ))}
            <span className="flex-1" />
            <button
              onClick={() => showPane(null)}
              title="Close  esc"
              aria-label="Close the side pane"
              className="size-7 rounded-lg text-[16px] text-muted-foreground hover:bg-raised hover:text-foreground"
            >
              ×
            </button>
          </>
        )}
      </div>
      <div className="min-h-0 flex-1">
        {editAsk ? (
          <Editor
            path={editAsk.subject ?? "document.md"}
            value={editAsk.default}
            readOnly={false}
            onChange={setDraft}
            onSave={() => answer(editAsk, draft)}
          />
        ) : pane === "changes" ? (
          <ChangesTab />
        ) : pane === "files" ? (
          <FilesTab />
        ) : pane === "memory" ? (
          <MemoryTab />
        ) : (
          <TerminalTab />
        )}
      </div>
    </aside>
  );
}
