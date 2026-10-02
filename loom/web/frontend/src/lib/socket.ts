import { useSession } from "../store/session";
import type { ClientEvent, ServerEvent } from "./protocol";

let socket: WebSocket | null = null;
let retryDelay = 500;

const MAX_RETRY_DELAY = 5000;

function url() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws`;
}

// Connect to loom, and keep reconnecting if the connection drops.
export function connect() {
  const store = useSession.getState();
  store.setConnection("connecting");

  const ws = new WebSocket(url());
  socket = ws;

  ws.onopen = () => {
    retryDelay = 500;
    store.reset();
    store.setConnection("open");
  };

  ws.onmessage = (message) => {
    let event: ServerEvent;
    try {
      event = JSON.parse(message.data);
    } catch {
      return;
    }
    useSession.getState().apply(event);
  };

  ws.onclose = () => {
    if (socket !== ws) return;
    socket = null;
    store.setConnection("closed");
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, MAX_RETRY_DELAY);
  };
}

export function send(event: ClientEvent) {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(event));
  return true;
}
