// Dark by default, like Claude Code; /theme switches to light and back. The choice is
// remembered in this browser, when it lets the page store it.

export type Theme = "dark" | "light";

const KEY = "loom.theme";

export function savedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function currentTheme(): Theme {
  return document.documentElement.classList.contains("light") ? "light" : "dark";
}

export function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("light", theme === "light");
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // Not stored: it still applies until the page reloads
  }
}
