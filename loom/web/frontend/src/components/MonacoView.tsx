import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/basic-languages/monaco.contribution.js";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";
import { useEffect, useRef } from "react";

// Monaco, from the local package so loom works offline. This module is loaded on demand,
// so the editor only downloads when the side pane first shows a file.

self.MonacoEnvironment = { getWorker: () => new EditorWorker() };

const FONT = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace';

monaco.editor.defineTheme("loom-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#1a1a1a",
    "editor.foreground": "#e5e5e5",
    "editorLineNumber.foreground": "#5a5a5a",
    "editorLineNumber.activeForeground": "#8a8a8a",
    "editor.lineHighlightBackground": "#1a1a1a",
    "editor.selectionBackground": "#d977574d",
    "editorCursor.foreground": "#d97757",
    "editorWidget.border": "#2a2a2a",
    "scrollbarSlider.background": "#2a2a2a80",
  },
});

monaco.editor.defineTheme("loom-light", {
  base: "vs",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#f8f6f2",
    "editor.lineHighlightBackground": "#f8f6f2",
    "editor.selectionBackground": "#c9603d40",
    "editorCursor.foreground": "#c9603d",
  },
});

function theme() {
  return document.documentElement.classList.contains("light") ? "loom-light" : "loom-dark";
}

function languageFor(path: string) {
  const name = (path.split("/").pop() ?? path).toLowerCase();
  const found = monaco.languages
    .getLanguages()
    .find(
      (language) =>
        language.filenames?.some((f) => f.toLowerCase() === name) ||
        language.extensions?.some((ext) => name.endsWith(ext.toLowerCase())),
    );
  return found?.id ?? "plaintext";
}

export default function MonacoView({
  path,
  value,
  readOnly,
  onChange,
  onSave,
}: {
  path: string;
  value: string;
  readOnly: boolean;
  onChange?: (value: string) => void;
  onSave?: () => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const editor = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const callbacks = useRef({ onChange, onSave });
  callbacks.current = { onChange, onSave };

  useEffect(() => {
    const ed = monaco.editor.create(container.current!, {
      theme: theme(),
      fontFamily: FONT,
      fontSize: 13,
      lineHeight: 20,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      renderLineHighlight: "none",
      automaticLayout: true,
      wordWrap: "on",
      padding: { top: 8 },
      overviewRulerLasso: false,
      overviewRulerBorder: false,
      hideCursorInOverviewRuler: true,
      scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
      // Type into a plain hidden textarea: the EditContext API it uses by default misses
      // some kinds of input
      editContext: false,
    } as monaco.editor.IStandaloneEditorConstructionOptions);
    editor.current = ed;
    ed.onDidChangeModelContent(() => callbacks.current.onChange?.(ed.getValue()));
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => callbacks.current.onSave?.());

    // Follow the light/dark toggle
    const observer = new MutationObserver(() => monaco.editor.setTheme(theme()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      observer.disconnect();
      ed.getModel()?.dispose();
      ed.dispose();
      editor.current = null;
    };
  }, []);

  useEffect(() => {
    const ed = editor.current;
    if (!ed) return;
    const old = ed.getModel();
    ed.setModel(monaco.editor.createModel(value, languageFor(path)));
    old?.dispose();
  }, [path, value]);

  useEffect(() => {
    editor.current?.updateOptions({ readOnly, domReadOnly: readOnly });
    if (!readOnly) editor.current?.focus();
  }, [readOnly]);

  return <div ref={container} className="h-full min-h-0 w-full" />;
}
