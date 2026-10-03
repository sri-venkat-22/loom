import { useSession } from "../store/session";

// "bedrock/global.moonshotai.kimi-k3" reads as "kimi-k3": no provider, region or vendor
export function shortModel(name: string) {
  let short = name.split("/").pop() ?? name;
  while (/^[a-z]+\.[a-z]/.test(short)) short = short.slice(short.indexOf(".") + 1);
  return short;
}

// Under the input: whether loom is ready, the tokens used, and the main shortcuts
export function StatusLine() {
  const connection = useSession((state) => state.connection);
  const busy = useSession((state) => state.session?.busy ?? false);
  const tokens = useSession((state) =>
    state.session ? state.session.tokens.sent + state.session.tokens.received : 0,
  );

  let status: string = connection;
  if (connection === "open") status = busy ? "working" : "ready";
  else if (connection === "closed") status = "disconnected";

  return (
    <div className="flex gap-3.5 overflow-hidden whitespace-nowrap px-1.5 pt-2 text-[12px] text-dim">
      <span className={connection === "closed" ? "text-destructive" : undefined}>{status}</span>
      <span>{tokens.toLocaleString()} tokens</span>
      <span>⌘K commands</span>
      <span>⌘B side pane</span>
    </div>
  );
}
