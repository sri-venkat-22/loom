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
    // Shiki's grammars are their own chunks, loaded only for files in that language; the
    // C++ one is the largest at about 800 kB
    chunkSizeWarningLimit: 1000,
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/ws": { target: `ws://${LOOM}`, ws: true },
      "/api": { target: `http://${LOOM}` },
    },
  },
});
