// The backend's JSON API, which is served under /api

export interface Health {
  status: string;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`/api/${path}`);
  if (!response.ok) throw new Error(`GET /api/${path}: ${response.status}`);
  return response.json();
}

export const api = {
  health: () => get<Health>("health"),
};
