// services/full-stack-agent-app/src/lib/theme.ts
export const THEME_STORAGE_KEY = "dip_theme";
export type Theme = "light" | "dark";

export function getSystemTheme(): Theme {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function getStoredTheme(): Theme | null {
  if (typeof window === "undefined") return null;
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "dark" || stored === "light" ? stored : null;
}

// Persists the user's EXPLICIT choice, overriding system preference from
// this point forward -- this is what makes it a manual toggle rather than
// Tailwind's default automatic media-query behavior.
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  window.localStorage.setItem(THEME_STORAGE_KEY, theme);
}