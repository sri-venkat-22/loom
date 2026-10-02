import { memo, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { type Token, languageForFence, useHighlighted } from "../lib/highlight";

// loom's replies are Markdown, shown in the terminal's style: monospace throughout, bold
// headings rather than big ones, and code blocks highlighted with shiki. Raw HTML in a
// reply is left out, never rendered.

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const lines = useMemo(() => code.replace(/\n$/, "").split("\n"), [code]);
  const grammar = languageForFence(lang);
  const tokens = useHighlighted(lines, "", grammar ?? "");
  return (
    <pre className="scroll-thin my-2 overflow-x-auto border border-border px-3 py-2 font-mono text-[13px] leading-5">
      {lines.map((line, i) => (
        <div key={i} className="min-h-5 whitespace-pre">
          {tokens?.[i] ? <Tokens tokens={tokens[i]} /> : line}
        </div>
      ))}
    </pre>
  );
}

function Tokens({ tokens }: { tokens: Token[] }) {
  return (
    <>
      {tokens.map((token, i) => (
        <span key={i} className="shiki-token" style={token.style}>
          {token.text}
        </span>
      ))}
    </>
  );
}

const heading = "mt-4 mb-2 font-bold text-foreground first:mt-0";

const COMPONENTS: Components = {
  h1: ({ children }) => <h1 className={heading}>{children}</h1>,
  h2: ({ children }) => <h2 className={heading}>{children}</h2>,
  h3: ({ children }) => <h3 className={heading}>{children}</h3>,
  h4: ({ children }) => <h4 className={heading}>{children}</h4>,
  h5: ({ children }) => <h5 className={heading}>{children}</h5>,
  h6: ({ children }) => <h6 className={heading}>{children}</h6>,
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-bold text-foreground">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="text-dim">{children}</del>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-primary underline">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="my-2 list-disc pl-6 marker:text-dim">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal pl-6 marker:text-dim">{children}</ol>,
  li: ({ children }) => <li className="my-0.5 pl-1">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-border" />,
  table: ({ children }) => (
    <div className="scroll-thin my-2 overflow-x-auto">
      <table className="border-collapse text-[14px]">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border px-3 py-1 text-left font-bold">{children}</th>
  ),
  td: ({ children }) => <td className="border border-border px-3 py-1">{children}</td>,
  pre: ({ children }) => <>{children}</>,
  code: ({ className, children }) => {
    const text = String(children ?? "");
    const fence = /language-([\w+#-]+)/.exec(className ?? "");
    // A fenced block, with or without a language
    if (fence || text.includes("\n")) return <CodeBlock code={text} lang={fence?.[1] ?? ""} />;
    return <code className="text-code">{children}</code>;
  },
};

const PLUGINS = [remarkGfm];

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="break-words">
      <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
});
