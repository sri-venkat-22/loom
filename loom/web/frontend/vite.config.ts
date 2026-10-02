import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the frontend with hot reload and passes /ws and /api through to a
// `loom --web --no-browser` running on the default port.
const LOOM = "127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Served by loom/web/backend/app.py, and packaged with loom
    outDir: "../static",
    emptyOutDir: true,
    // Monaco (about 2.7 MB) and shiki's grammars (C++ is about 800 kB) are their own
    // chunks, loaded from this computer only when the side pane or a diff needs them
    chunkSizeWarningLimit: 3000,
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/ws": { target: `ws://${LOOM}`, ws: true },
      "/api": { target: `http://${LOOM}` },
    },
  },
});
