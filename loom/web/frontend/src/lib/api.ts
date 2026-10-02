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

export interface Decision {
  id: number;
  time: string;
  phase: string | null;
  source: string;
  kind: string;
  text: string;
  reason: string | null;
}

export interface Memory {
  available: boolean;
  store: string | null;
  query: string;
  hits: MemoryHit[];
  decisions: Decision[];
}

export interface Alias {
  alias: string;
  model: string;
}

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
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
  return response.json();
}

export const api = {
  files: () => get<Files>("files"),
  file: (path: string) => get<FileText>("file", { path }),
  memory: (q: string) => get<Memory>("memory", { q }),
  models: () => get<{ aliases: Alias[] }>("models"),
};
