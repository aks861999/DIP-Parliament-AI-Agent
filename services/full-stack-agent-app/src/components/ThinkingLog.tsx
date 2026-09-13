// services/full-stack-agent-app/src/components/ThinkingLog.tsx
"use client";

export default function ThinkingLog({ steps }: { steps: string[] }) {
  if (!steps || steps.length === 0) return null;
  return (
  <details className="mb-2 rounded-lg border-l-4 border-accent bg-amber-50 open:bg-amber-100 dark:bg-amber-950 dark:open:bg-amber-900 px-3 py-2 text-sm transition-colors">
    <summary className="cursor-pointer font-semibold text-amber-900 dark:text-amber-100">🧠 Thinking</summary>
    <ul className="mt-2 space-y-1 text-neutral-700 dark:text-neutral-300">
        {steps.map((s, i) => (
          <li key={i}>- {s}</li>
        ))}
      </ul>
    </details>
  );
}