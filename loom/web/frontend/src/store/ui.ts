import { create } from "zustand";

// The side pane's tabs. The editor shows only while loom waits for a document's edit.
export type Pane = "files" | "memory" | "terminal" | "model";

interface UiState {
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
  pane: null,
  openFile: null,
  memoryQuery: "",
  paletteRequests: 0,
  openPalette: () => set((state) => ({ paletteRequests: state.paletteRequests + 1 })),
  showPane: (pane) => set({ pane }),
  togglePane: () => set((state) => ({ pane: state.pane ? null : "files" })),
  openInViewer: (path) => set({ pane: "files", openFile: path }),
  setMemoryQuery: (memoryQuery) => set({ memoryQuery }),
}));
