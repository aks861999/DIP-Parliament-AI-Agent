// services/full-stack-agent-app/src/components/ThemeToggle.tsx
"use client";

import { useEffect, useState } from "react";
import { applyTheme, getStoredTheme, getSystemTheme } from "@/lib/theme";

export default function ThemeToggle() {
  const [isDark, setIsDark] = useState(false);

  // Reads whatever the blocking inline script in layout.tsx already
  // applied to <html> on first paint, so this button's icon matches
  // reality immediately instead of flashing the wrong state.
  useEffect(() => {
    const initial = getStoredTheme() ?? getSystemTheme();
    setIsDark(initial === "dark");
  }, []);

  function toggle() {
    const next = isDark ? "light" : "dark";
    applyTheme(next);
    setIsDark(!isDark);
  }

  return (
    <button
      onClick={toggle}
      aria-label="Toggle dark mode"
      className="rounded-lg border border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 p-2 text-sm shadow-sm hover:bg-neutral-100 dark:hover:bg-neutral-800"
    >
      {isDark ? "☀️" : "🌙"}
    </button>
  );
}