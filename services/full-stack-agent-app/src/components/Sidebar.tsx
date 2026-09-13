// services/full-stack-agent-app/src/components/Sidebar.tsx
"use client";

import { useEffect, useState } from "react";
import type { ChatThreadRow } from "@/lib/db";


interface Props {
  activeThreadId: string;
  onNewChat: () => void;
  onSelectThread: (id: string) => void;
  onClose: () => void;
  refreshKey: number;
}


export default function Sidebar({ activeThreadId, onNewChat, onSelectThread, onClose, refreshKey }: Props) {
  const [threads, setThreads] = useState<ChatThreadRow[]>([]);

  useEffect(() => {
    fetch("/api/threads")
      .then((r) => r.json())
      .then(setThreads)
      .catch(() => setThreads([]));
  }, [refreshKey]);

  return (
  <aside className="w-72 max-w-[85vw] shrink-0 border-r border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 dark:text-neutral-100 p-4 flex flex-col gap-4 h-full overflow-y-auto">
        <div className="flex items-center justify-between">
        <button onClick={onNewChat} className="flex-1 rounded-lg border border-neutral-300 dark:border-neutral-700 px-3 py-2 text-sm font-medium hover:bg-neutral-100 dark:hover:bg-neutral-800">
          + New chat
        </button>
        <button onClick={onClose} aria-label="Close sidebar"
          className="ml-2 rounded-lg p-2 text-neutral-500 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800">
          ✕
        </button>
        </div>

      <p className="text-xs text-neutral-400">
        Public demo, every visitor can see every thread here.
      </p>

      <details className="text-sm">
        <summary className="cursor-pointer font-medium">ℹ️ About this data</summary>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">
          Answers come from <strong>DIP</strong>, the Bundestag&apos;s own open-data API — no
          general knowledge, no guessing. Covers politicians&apos; bios/party history, party
          seat composition by <em>Wahlperiode</em> (electoral term), and who holds a given
          office. It doesn&apos;t cover current events, opinions, or non-German politics.
        </p>
      </details>

      <details className="text-sm">
        <summary className="cursor-pointer font-medium">🔍 How this agent stays honest</summary>
        <ul className="mt-2 space-y-1 text-neutral-600 dark:text-neutral-400 list-disc list-inside">
          <li>
            🧭 <strong>Classified as</strong> — how your question was routed
          </li>
          <li>
            🔧 <strong>Tool call</strong> — a real, live query to DIP
          </li>
          <li>
            🔍 <strong>Completeness check</strong> — verifies nothing needed is missing
          </li>
          <li>
            ✅ <strong>Faithfulness check</strong> — verifies the answer matches the data
          </li>
        </ul>
      </details>

      <div className="border-t border-neutral-200 pt-3 flex-1 overflow-y-auto">
        <p className="text-xs uppercase text-neutral-400 mb-2">Conversations</p>
        {threads.map((t) => (
          <button
            key={t.thread_id}
            onClick={() => onSelectThread(t.thread_id)}
            className={`block w-full truncate rounded-lg px-3 py-2 text-left text-sm mb-1 ${
              t.thread_id === activeThreadId
                ? "bg-neutral-200 dark:bg-neutral-800"
                : "hover:bg-neutral-100 dark:hover:bg-neutral-800"
            }`}
          >
            {t.title || "Untitled conversation"}
          </button>
        ))}
      </div>
    </aside>
  );
}