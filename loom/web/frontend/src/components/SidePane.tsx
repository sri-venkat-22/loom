import { Suspense, lazy, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { type Alias, type Files, type Memory, api } from "../lib/api";
import { answer } from "../lib/asks";
import { send } from "../lib/socket";
import { pendingAsk, useSession } from "../store/session";
import { type Pane, useUi } from "../store/ui";

const MonacoView = lazy(() => import("./MonacoView"));

const TABS: Pane[] = ["files", "memory", "terminal", "model"];

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
    <Suspense fallback={<div className="p-4 text-dim">loading editor…</div>}>
      <MonacoView {...props} />
    </Suspense>
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
        // Open the folders of the files in the chat
        setExpanded((before) => {
          const next = new Set(before);
          for (const path of found.chat) {
            const parts = path.split("/");
            for (let i = 1; i < parts.length; i++) next.add(parts.slice(0, i).join("/"));
          }
          return next;
        });
      })
      .catch((err: Error) => setError(err.message));
  }, [chatFiles]);

  const tree = useMemo(() => buildTree(files?.files ?? []), [files]);
  const inChat = useMemo(() => new Set(files?.chat ?? []), [files]);
  const readOnly = useMemo(() => new Set(files?.read_only ?? []), [files]);

  function toggle(path: string) {
    if (!idle) return;
    const command = inChat.has(path) || readOnly.has(path) ? "/drop" : "/add";
    send({ type: "input", text: `${command} ${quote(path)}` });
  }

  function rows(nodes: TreeNode[], depth: number): React.ReactNode[] {
    return nodes.flatMap((node) => {
      const pad = { paddingLeft: depth * 14 };
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
            className="flex w-full items-center gap-2 py-px text-left text-dim hover:text-foreground"
          >
            <span className="w-6 shrink-0">{open ? "▾" : "▸"}</span>
            {node.name}/
          </button>,
          ...(open ? rows(node.children, depth + 1) : []),
        ];
      }
      const included = inChat.has(node.path);
      const ro = readOnly.has(node.path);
      return [
        <div key={node.path} style={pad} className="flex items-center gap-2 py-px">
          <button
            onClick={() => toggle(node.path)}
            disabled={!idle}
            title={included ? "in the chat: click to drop" : ro ? "read-only" : "add to the chat"}
            className={`w-6 shrink-0 text-left ${included || ro ? "text-primary" : "text-dim"} enabled:hover:text-foreground`}
          >
            {included ? "[x]" : ro ? "[r]" : "[ ]"}
          </button>
          <button
            onClick={() => openInViewer(node.path)}
            className={`min-w-0 truncate text-left ${
              openFile === node.path
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground"
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
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-4">
        {error && <div className="text-destructive">{error}</div>}
        {files && !files.files.length && <div className="text-dim">No files.</div>}
        {rows(tree, 0)}
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
    <div className="flex h-[55%] min-h-0 flex-col border-t border-border">
      <div className="truncate px-4 py-2 text-[12px] text-dim">{path}</div>
      <div className="min-h-0 flex-1">
        {error && <div className="px-4 text-destructive">{error}</div>}
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

  return (
    <div className="scroll-thin h-full overflow-y-auto p-4">
      <input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="semantic search…"
        className="mb-3 w-full border-b border-border bg-transparent pb-1 outline-none placeholder:text-dim"
      />
      {searching && <div className="mb-2 text-[12px] text-dim">searching…</div>}
      {error && <div className="text-destructive">{error}</div>}
      {memory && !memory.available && (
        <div className="text-dim">
          No project memory yet. Start a project with /project new IDEA.
        </div>
      )}
      {memory?.store && <div className="mb-2 text-[12px] text-dim">{memory.store}</div>}
      {memory?.available && query.trim() && !memory.hits.length && (
        <div className="text-dim">Nothing matches.</div>
      )}
      {memory?.hits.map((hit) => (
        <div key={hit.id} className="border-b border-border py-2.5">
          <div className="flex gap-3 text-[12px] text-dim">
            <span className="truncate">{hit.source ?? hit.id}</span>
            <span className="text-primary">{hit.kind}</span>
            {hit.phase && <span className="ml-auto">{hit.phase}</span>}
          </div>
          {hit.title && <div className="mt-1 text-foreground">{hit.title}</div>}
          <div className="mt-1 line-clamp-6 whitespace-pre-wrap break-words text-muted-foreground">
            {hit.text}
          </div>
        </div>
      ))}
      {memory?.available && !query.trim() && !memory.decisions.length && (
        <div className="text-dim">No decisions recorded yet.</div>
      )}
      {!query.trim() &&
        memory?.decisions.map((decision) => (
          <div key={decision.id} className="border-b border-border py-2.5">
            <div className="flex gap-3 text-[12px] text-dim">
              <span>d-{decision.id}</span>
              <span className="text-primary">{decision.kind}</span>
              <span className="truncate">{decision.source}</span>
              <span className="ml-auto shrink-0">{decision.phase}</span>
            </div>
            <div className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">
              {decision.text}
            </div>
            {decision.reason && <div className="mt-1 text-dim">why: {decision.reason}</div>}
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
    <div ref={scroller} className="scroll-thin h-full overflow-y-auto p-4">
      {!terminal && <div className="text-dim">Output of /run shows here.</div>}
      <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-5 text-muted-foreground">
        {terminal}
        {running && <span className="blink">▍</span>}
      </pre>
    </div>
  );
}

// Model

function ModelTab() {
  const session = useSession((state) => state.session);
  const idle = useIdle();
  const [aliases, setAliases] = useState<Alias[]>([]);
  const [main, setMain] = useState("");
  const [weak, setWeak] = useState("");

  useEffect(() => {
    api
      .models()
      .then((found) => setAliases(found.aliases))
      .catch(() => setAliases([]));
  }, []);

  function switchTo(command: string, model: string) {
    if (!idle || !model.trim()) return;
    send({ type: "input", text: `${command} ${model.trim()}` });
  }

  const field = (
    label: string,
    current: string,
    value: string,
    setValue: (v: string) => void,
    command: string,
  ) => (
    <div>
      <div className="mb-2 text-dim">{label} model</div>
      <div className="text-primary">› {current || "(none)"}</div>
      <input
        value={value}
        disabled={!idle}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            switchTo(command, value);
            setValue("");
          }
        }}
        placeholder="switch to…"
        className="mt-2 w-full border-b border-border bg-transparent pb-1 outline-none placeholder:text-dim"
      />
    </div>
  );

  return (
    <div className="scroll-thin h-full space-y-6 overflow-y-auto p-4">
      {field("main", session?.model ?? "", main, setMain, "/model")}
      {field("weak", session?.weak_model ?? "", weak, setWeak, "/weak-model")}
      {aliases.length > 0 && (
        <div>
          <div className="mb-2 text-dim">aliases</div>
          {aliases.map((alias) => (
            <button
              key={alias.alias}
              disabled={!idle}
              onClick={() => switchTo("/model", alias.alias)}
              className="flex w-full gap-3 py-0.5 text-left text-muted-foreground enabled:hover:text-foreground"
            >
              <span className="w-28 shrink-0">{alias.alias}</span>
              <span className="truncate text-dim">{alias.model}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Editing a /project document at a checkpoint

function DocumentEditor() {
  const entries = useSession((state) => state.entries);
  const ask = pendingAsk(entries)?.ask;
  const [value, setValue] = useState("");

  useEffect(() => {
    if (ask?.kind === "edit") setValue(ask.default);
  }, [ask]);

  if (!ask || ask.kind !== "edit") return null;
  const save = () => answer(ask, value);
  const cancel = () => answer(ask, ask.default);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-4 px-4 py-2 text-[12px]">
        <span className="min-w-0 truncate text-dim">editing {ask.subject}</span>
        <button onClick={save} className="ml-auto text-primary hover:underline">
          save ⌘S
        </button>
        <button onClick={cancel} className="text-muted-foreground hover:text-foreground">
          cancel
        </button>
      </div>
      <div className="min-h-0 flex-1 border-t border-border">
        <Editor
          path={ask.subject ?? "document.md"}
          value={ask.default}
          readOnly={false}
          onChange={setValue}
          onSave={save}
        />
      </div>
    </div>
  );
}

export function SidePane() {
  const pane = useUi((state) => state.pane);
  const showPane = useUi((state) => state.showPane);
  const editing = useSession((state) => pendingAsk(state.entries)?.ask.kind === "edit");

  if (!pane && !editing) return null;

  return (
    <aside className="flex w-[420px] shrink-0 flex-col border-l border-border text-[13px]">
      <div className="flex items-center gap-4 border-b border-border px-4 py-2 text-[12px]">
        {editing ? (
          <span className="text-primary">editor</span>
        ) : (
          TABS.map((tab) => (
            <button
              key={tab}
              onClick={() => showPane(tab)}
              className={tab === pane ? "text-primary" : "text-dim hover:text-foreground"}
            >
              {tab}
            </button>
          ))
        )}
        {!editing && (
          <button onClick={() => showPane(null)} className="ml-auto text-dim hover:text-foreground">
            esc ×
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1">
        {editing ? (
          <DocumentEditor />
        ) : pane === "files" ? (
          <FilesTab />
        ) : pane === "memory" ? (
          <MemoryTab />
        ) : pane === "terminal" ? (
          <TerminalTab />
        ) : (
          <ModelTab />
        )}
      </div>
    </aside>
  );
}
