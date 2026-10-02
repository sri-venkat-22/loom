import { useEffect, useState } from "react";
import type { HighlighterCore, LanguageRegistration } from "shiki/core";

// Syntax highlighting for diffs, with shiki. It loads on first use, and each language
// loads when a file needs it, so the page itself stays small.

export interface Token {
  text: string;
  // CSS custom properties: --shiki-dark and --shiki-light, picked by the theme in CSS
  style?: Record<string, string>;
}

type Grammar = () => Promise<{ default: LanguageRegistration[] }>;

const GRAMMARS: Record<string, Grammar> = {
  python: () => import("shiki/langs/python.mjs"),
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  jsx: () => import("shiki/langs/jsx.mjs"),
  json: () => import("shiki/langs/json.mjs"),
  markdown: () => import("shiki/langs/markdown.mjs"),
  yaml: () => import("shiki/langs/yaml.mjs"),
  toml: () => import("shiki/langs/toml.mjs"),
  shellscript: () => import("shiki/langs/shellscript.mjs"),
  css: () => import("shiki/langs/css.mjs"),
  scss: () => import("shiki/langs/scss.mjs"),
  html: () => import("shiki/langs/html.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
  java: () => import("shiki/langs/java.mjs"),
  kotlin: () => import("shiki/langs/kotlin.mjs"),
  swift: () => import("shiki/langs/swift.mjs"),
  c: () => import("shiki/langs/c.mjs"),
  cpp: () => import("shiki/langs/cpp.mjs"),
  ruby: () => import("shiki/langs/ruby.mjs"),
  php: () => import("shiki/langs/php.mjs"),
  sql: () => import("shiki/langs/sql.mjs"),
  dockerfile: () => import("shiki/langs/dockerfile.mjs"),
};

const EXTENSIONS: Record<string, string> = {
  py: "python",
  pyi: "python",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "tsx",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  json: "json",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
  toml: "toml",
  sh: "shellscript",
  bash: "shellscript",
  zsh: "shellscript",
  css: "css",
  scss: "scss",
  html: "html",
  htm: "html",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  hpp: "cpp",
  rb: "ruby",
  php: "php",
  sql: "sql",
};

export function languageFor(file: string): string | undefined {
  const name = file.split("/").pop() ?? file;
  if (name === "Dockerfile") return "dockerfile";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return EXTENSIONS[ext];
}

let highlighter: Promise<HighlighterCore> | null = null;
const loading = new Map<string, Promise<void>>();

function getHighlighter() {
  highlighter ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
      import("shiki/core"),
      import("shiki/engine/javascript"),
    ]);
    return createHighlighterCore({
      themes: [
        import("shiki/themes/github-dark-default.mjs"),
        import("shiki/themes/github-light-default.mjs"),
      ],
      langs: [],
      engine: createJavaScriptRegexEngine(),
    });
  })();
  return highlighter;
}

async function highlightLines(lines: string[], lang: string): Promise<Token[][]> {
  const shiki = await getHighlighter();
  if (!loading.has(lang))
    loading.set(
      lang,
      GRAMMARS[lang]().then((m) => shiki.loadLanguage(m.default)),
    );
  await loading.get(lang);
  const { tokens } = shiki.codeToTokens(lines.join("\n"), {
    lang,
    themes: { dark: "github-dark-default", light: "github-light-default" },
    defaultColor: false,
  });
  return tokens.map((line) =>
    line.map((token) => ({
      text: token.content,
      style: token.htmlStyle as Record<string, string> | undefined,
    })),
  );
}

// The tokens of each line of code from file, once they're ready; null until then, or for
// files in languages without a grammar here.
export function useHighlighted(lines: string[], file: string): Token[][] | null {
  const lang = languageFor(file);
  const key = lines.join("\n");
  const [result, setResult] = useState<{ key: string; tokens: Token[][] } | null>(null);

  useEffect(() => {
    if (!lang) return;
    let live = true;
    highlightLines(key.split("\n"), lang)
      .then((tokens) => live && setResult({ key, tokens }))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [key, lang]);

  return result?.key === key ? result.tokens : null;
}
