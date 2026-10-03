import { useEffect, useState } from "react";

import { useSession } from "../store/session";
import { type Changes, api } from "./api";

// The code changes since loom started or since main, read again whenever a tool call ends
// or loom finishes a request, either of which may have changed files.
export function useChanges(base: "session" | "branch") {
  const [changes, setChanges] = useState<Changes | null>(null);
  const [error, setError] = useState("");
  const connected = useSession((state) => state.connection === "open");
  const busy = useSession((state) => state.session?.busy ?? false);
  const finished = useSession(
    (state) => state.entries.filter((e) => e.kind === "tool" && e.status !== "running").length,
  );

  useEffect(() => {
    if (!connected) return;
    let live = true;
    const timer = setTimeout(() => {
      api
        .changes(base)
        .then((found) => live && (setChanges(found), setError("")))
        .catch((err: Error) => live && setError(err.message));
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [base, connected, busy, finished]);

  return { changes, error };
}

export function changeTotals(changes: Changes | null) {
  const files = changes?.files ?? [];
  return {
    added: files.reduce((n, f) => n + f.added, 0),
    removed: files.reduce((n, f) => n + f.removed, 0),
  };
}
