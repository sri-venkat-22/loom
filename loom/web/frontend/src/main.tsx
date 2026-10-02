import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { connect } from "./lib/socket";
import { applyTheme, savedTheme } from "./lib/theme";
import "./styles/index.css";

// Before the first render, so a light page doesn't flash dark
applyTheme(savedTheme());
connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
