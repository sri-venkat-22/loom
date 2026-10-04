// The links on the web tools' cards, read from what the model got back: web_search's
// results are numbered markdown links (loom/websearch.py format_results), and web_fetch's
// page is the URL in its arguments, or the one it was redirected to.

export interface WebLink {
  title: string;
  url: string;
  snippet: string;
}

const RESULT = /^\d+\. \[(.*)\]\((https?:\/\/[^\s)]+)\)$/;
const REDIRECTED = /^\(Redirected to (https?:\/\/\S+?)\.\)$/m;

export function searchResults(output: string): WebLink[] {
  const links: WebLink[] = [];
  for (const line of output.split("\n")) {
    const match = RESULT.exec(line);
    if (match) {
      links.push({ title: match[1], url: match[2], snippet: "" });
    } else if (links.length && line.startsWith("   ") && !links[links.length - 1].snippet) {
      links[links.length - 1].snippet = line.trim();
    }
  }
  return links;
}

export function fetchedPage(args: string | null, output: string): WebLink | null {
  let url = "";
  try {
    url = JSON.parse(args ?? "{}").url ?? "";
  } catch {
    return null;
  }
  const redirected = REDIRECTED.exec(output);
  if (redirected) url = redirected[1];
  if (!/^https?:\/\//.test(url)) return null;
  return { title: url, url, snippet: "" };
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
