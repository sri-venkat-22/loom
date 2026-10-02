import { create } from "zustand";

// The side pane's tabs. The editor shows only while loom waits for a document's edit.
export type Pane = "changes" | "files" | "memory" | "terminal" | "model";

const SIDEBAR_KEY = "loom.sidebar";

// The sessions sidebar starts open on wide windows, or as the user last left it
function sidebarAtStart() {
  try {
    const saved = localStorage.getItem(SIDEBAR_KEY);
    if (saved) return saved === "open";
  } catch {
    // No storage: use the default
  }
  return window.innerWidth >= 1100;
}

interface UiState {
  // The sessions sidebar, on the left
  sidebar: boolean;
  toggleSidebar: () => void;
  // The open tab, or null when the pane is hidden (the default)
  pane: Pane | null;
  // The file the viewer shows
  openFile: string | null;
  memoryQuery: string;
  // Counts ⌘K presses, which open the command menu in the input
  paletteRequests: number;
  openPalette: () => void;
  showPane: (pane: Pane | null) => void;
  togglePane: () => void;
  openInViewer: (path: string) => void;
  setMemoryQuery: (query: string) => void;
}

export const useUi = create<UiState>((set) => ({
  sidebar: sidebarAtStart(),
  toggleSidebar: () =>
    set((state) => {
      try {
        localStorage.setItem(SIDEBAR_KEY, state.sidebar ? "closed" : "open");
      } catch {
        // Not remembered
      }
      return { sidebar: !state.sidebar };
    }),
  pane: null,
  openFile: null,
  memoryQuery: "",
  paletteRequests: 0,
  openPalette: () => set((state) => ({ paletteRequests: state.paletteRequests + 1 })),
  showPane: (pane) => set({ pane }),
  togglePane: () => set((state) => ({ pane: state.pane ? null : "changes" })),
  openInViewer: (path) => set({ pane: "files", openFile: path }),
  setMemoryQuery: (memoryQuery) => set({ memoryQuery }),
}));
