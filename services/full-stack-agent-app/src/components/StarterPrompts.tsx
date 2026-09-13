// services/full-stack-agent-app/src/components/StarterPrompts.tsx
"use client";

const EXAMPLE_PROMPTS: Record<string, string[]> = {
  Politicians: ["Who is Angela Merkel?", "Who is Friedrich Merz?"],
  Parties: [
    "Party distribution in Wahlperiode 20",
    "Compare CDU/CSU across Wahlperiode 19, 20, and 21",
  ],
  Roles: ["Who is the current Bundeskanzler?"],
};

export default function StarterPrompts({ onSelect }: { onSelect: (q: string) => void }) {
  return (
    <div className="max-w-2xl mx-auto text-center">
      <h1 className="text-2xl font-semibold mb-2">AI Agent for Dokumentations- und Informationssystems für Parlamentsmaterialien</h1>
      <p className="text-neutral-600 dark:text-neutral-400 mb-6">
        I answer questions about the German Bundestag using <strong>DIP</strong>, the
        Bundestag&apos;s own open-data API, politicians&apos; bios and party history, party
        seat composition by Wahlperiode (electoral term), and who holds a given office. I
        don&apos;t answer from general knowledge; every fact traces back to a live DIP query.
        Try one of the examples below, or ask your own.
      </p>
      {Object.entries(EXAMPLE_PROMPTS).map(([category, prompts]) => (
        <div key={category} className="mb-4">
          <p className="text-xs uppercase text-neutral-400 dark:text-neutral-500 mb-2">{category}</p>
          <div className="flex flex-wrap justify-center gap-2">
            {prompts.map((p) => (
              <button
                key={p}
                onClick={() => onSelect(p)}
                className="rounded-full border border-neutral-300 dark:border-neutral-700 border-l-4 border-l-accent px-4 py-2 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800"
              >
                {p}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}