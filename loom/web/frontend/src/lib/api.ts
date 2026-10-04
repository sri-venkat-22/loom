import type { CheckpointKind, Decision, DiffLine, RunOutcome, Timeline } from "./protocol";

export type { Decision };

// The side pane's reads of the project, from loom/web/backend/api.py.

export interface Files {
  root: string;
  files: string[];
  chat: string[];
  read_only: string[];
}

export interface FileText {
  path: string;
  text: string;
}

export interface MemoryHit {
  id: string;
  kind: string;
  phase: string | null;
  source: string | null;
  title: string | null;
  text: string;
  score: number;
}

export interface Memory {
  available: boolean;
  store: string | null;
  query: string;
  hits: MemoryHit[];
  decisions: Decision[];
}

export interface SavedSession {
  id: string;
  // ISO time it was last saved, or null for a new conversation not saved yet
  updated: string | null;
  title: string;
  messages: number;
  current: boolean;
}

export interface FileChange {
  path: string;
  old_path: string | null;
  status: "added" | "modified" | "deleted" | "renamed";
  binary: boolean;
  added: number;
  removed: number;
  lines: DiffLine[];
}

export interface Changes {
  available: boolean;
  base: string | null;
  label: string | null;
  files: FileChange[];
}

// What a run of a phase's agent changed
export interface PhaseDiff {
  available: boolean;
  key: string;
  // The run shown, and the runs that made commits to choose from
  run: number | null;
  base: string | null;
  head: string | null;
  runs: { run: number; started: string; commits: number; outcome: RunOutcome }[];
  files: FileChange[];
}

// What changed in the files since a checkpoint, for the rewind dialog
export interface CheckpointDetail {
  id: string;
  time: string | null;
  prompt: string;
  kind: CheckpointKind;
  conversation: boolean;
  git: boolean;
  // Why the code can't be rewound now, like a merge in progress
  busy: string | null;
  error: string | null;
  // changed in both, created since (a rewind deletes them), deleted since (it restores them)
  changes: { changed: string[]; created: string[]; deleted: string[] } | null;
}

export type ReportFormat = "md" | "html" | "docx" | "pdf";

export interface Alias {
  alias: string;
  model: string;
}

async function fetchOk(path: string, params?: Record<string, string>): Promise<Response> {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  const response = await fetch(`/api/${path}${query}`);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // Not JSON
    }
    throw new Error(detail);
  }
  return response;
}

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  return (await fetchOk(path, params)).json();
}

// The file name in a response's Content-Disposition, like report.docx
function attachmentName(response: Response, fallback: string) {
  const match = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "");
  return match ? match[1] : fallback;
}

// Download the /project report as format. Word may come as HTML when loom has no pandoc.
async function downloadReport(format: ReportFormat) {
  const response = await fetchOk("project/report", { format });
  const name = attachmentName(response, `report.${format}`);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return name;
}

export const api = {
  files: () => get<Files>("files"),
  file: (path: string) => get<FileText>("file", { path }),
  memory: (q: string) => get<Memory>("memory", { q }),
  models: () => get<{ aliases: Alias[] }>("models"),
  sessions: () => get<{ saved: boolean; sessions: SavedSession[] }>("sessions"),
  changes: (base: "session" | "branch") => get<Changes>("changes", { base }),
  timeline: () => get<Timeline>("project/timeline"),
  phaseDiff: (key: string, run?: number) =>
    get<PhaseDiff>(`project/phase/${encodeURIComponent(key)}/diff`, run ? { run: `${run}` } : {}),
  checkpoint: (id: string) => get<CheckpointDetail>(`checkpoints/${encodeURIComponent(id)}`),
  downloadReport,
};
