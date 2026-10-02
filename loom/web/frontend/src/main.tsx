import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { connect } from "./lib/socket";
import "./styles/index.css";

connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
