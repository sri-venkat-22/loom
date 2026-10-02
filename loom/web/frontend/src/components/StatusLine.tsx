import { useSession } from "../store/session";

// "bedrock/global.moonshotai.kimi-k3" reads as "kimi-k3": no provider, region or vendor
export function shortModel(name: string) {
  let short = name.split("/").pop() ?? name;
  while (/^[a-z]+\.[a-z]/.test(short)) short = short.slice(short.indexOf(".") + 1);
  return short;
}

export function StatusLine() {
  const connection = useSession((state) => state.connection);
  const session = useSession((state) => state.session);

  let status: string = connection;
  if (connection === "open") status = session?.busy ? "working" : "ready";
  else if (connection === "closed") status = "disconnected";

  const tokens = session ? session.tokens.sent + session.tokens.received : 0;

  return (
    <footer className="mx-auto flex w-full max-w-[868px] shrink-0 gap-x-5 whitespace-nowrap px-5 pb-[23px] pt-3 text-[13px] text-muted-foreground">
      <span className={connection === "closed" ? "text-destructive" : undefined}>{status}</span>
      {session?.model && <span title={session.model}>{shortModel(session.model)}</span>}
      <span>{tokens.toLocaleString()} tokens</span>
      {session?.cwd && (
        // Long paths lose their start, keeping the project's own name in view
        <span className="min-w-0 truncate [direction:rtl]" title={session.cwd}>
          &lrm;{session.cwd}&lrm;
        </span>
      )}
      {session?.phase && <span className="text-primary">{session.phase}</span>}
    </footer>
  );
}
