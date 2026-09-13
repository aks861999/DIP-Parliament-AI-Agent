// services/full-stack-agent-app/src/app/page.tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import type { ChatMessage, QueryResponse } from "@/lib/types";
import ThemeToggle from "@/components/ThemeToggle";

export default function HomePage() {
  const [threadId, setThreadId] = useState<string>("");
  const [persisted, setPersisted] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(true);


  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const existing = params.get("thread");
    if (existing) {
      setThreadId(existing);
      setPersisted(true);
      loadMessages(existing);
    } else {
      setThreadId(crypto.randomUUID());
      setPersisted(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadMessages(id: string) {
    try {
      const res = await fetch(`/api/threads/${id}`);
      if (res.ok) setMessages(await res.json());
    } catch {
      setMessages([]);
    }
  }

  function pushThreadParam(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set("thread", id);
    window.history.replaceState({}, "", url.toString());
  }

  const handleNewChat = useCallback(() => {
    const id = crypto.randomUUID();
    setThreadId(id);
    setPersisted(false);
    setMessages([]);
    pushThreadParam(id);
  }, []);

  const handleSelectThread = useCallback((id: string) => {
    setThreadId(id);
    setPersisted(true);
    pushThreadParam(id);
    loadMessages(id);
  }, []);

  const handleSubmit = useCallback(
    async (query: string) => {
      if (!persisted) {
        await fetch("/api/threads", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ thread_id: threadId, title: query }),
        });
        setPersisted(true);
        pushThreadParam(threadId);
        setRefreshKey((k) => k + 1);
      }

    setMessages((prev) => [...prev, { role: "user", content: query }]);
    setLoading(true);
    const startedAt = Date.now();
    try {
      const res = await fetch("/api/query", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ query, thread_id: threadId }),
        });
        const data: QueryResponse = await res.json();
        const generationSeconds = Math.round((Date.now() - startedAt) / 1000);
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content:
              data.answer ?? "Sorry, the backend took too long to respond. Please try again.",
            chart_data: data.party_distribution,
            thinking_log: data.thinking_log,
            faithfulness_score: data.faithfulness_score,
            completeness_score: data.completeness_score,
            generation_time_seconds: generationSeconds,
          },
        ]);
        await fetch(`/api/threads/${threadId}`, { method: "PATCH" });
        setRefreshKey((k) => k + 1);
      } catch {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: "Sorry, the backend took too long to respond. Please try again.",
          },
        ]);
      } finally {
        setLoading(false);
      }
    },
    [threadId, persisted]
  );

return (
  <div className="flex h-screen overflow-hidden dark:bg-neutral-950">
    <div className="fixed top-2 right-2 z-50">
      <ThemeToggle />
    </div>
    {sidebarOpen && (
      <>
        <div
          className="fixed inset-0 z-30 bg-black/30 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
        <div className="fixed inset-y-0 left-0 z-40 md:static md:z-auto">
          <Sidebar
            activeThreadId={threadId}
            onNewChat={handleNewChat}
            onSelectThread={handleSelectThread}
            onClose={() => setSidebarOpen(false)}
            refreshKey={refreshKey}
          />
        </div>
      </>
    )}
    <div className="flex flex-col flex-1 min-w-0 min-h-0">
      {!sidebarOpen && (
      <button
        onClick={() => setSidebarOpen(true)}
        aria-label="Open sidebar"
        className="m-2 w-fit rounded-lg border border-neutral-300 dark:border-neutral-700 p-2 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800 dark:text-neutral-200"
      >
          ☰
        </button>
      )}
      <ChatWindow messages={messages} loading={loading} onSubmit={handleSubmit} />
    </div>
  </div>
);
}