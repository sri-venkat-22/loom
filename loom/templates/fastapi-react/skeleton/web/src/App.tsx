import { useEffect, useState } from "react";

import { api } from "./api";

export function App() {
  const [status, setStatus] = useState("checking…");

  useEffect(() => {
    api
      .health()
      .then((health) => setStatus(health.status))
      .catch(() => setStatus("unreachable"));
  }, []);

  return (
    <main>
      <h1>app</h1>
      <p>API: {status}</p>
    </main>
  );
}
