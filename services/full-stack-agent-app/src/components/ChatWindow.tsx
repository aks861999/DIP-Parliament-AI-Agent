// services/full-stack-agent-app/src/components/ChatWindow.tsx
"use client";

import { useEffect, useRef, useState } from "react";
import MessageBubble from "./MessageBubble";
import StarterPrompts from "./StarterPrompts";
import type { ChatMessage } from "@/lib/types";
import LoadingIndicator from "./LoadingIndicator";

interface Props {
  messages: ChatMessage[];
  loading: boolean;
  onSubmit: (query: string) => void;
}
export default function ChatWindow({ messages, loading, onSubmit }: Props) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q) return;
    setInput("");
    onSubmit(q);
  }

  const inputBar = (
    <div className="w-full flex gap-2 px-6">
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="Ask about German parliamentary data..."
        className="flex-1 rounded-full border border-neutral-300 dark:border-neutral-700 bg-white dark:bg-neutral-900 dark:text-neutral-100 px-4 py-3 text-sm outline-none focus:border-accent"
      />
      <button type="submit" disabled={loading}
        className="rounded-full bg-black px-5 py-3 text-sm font-medium text-white disabled:opacity-40">
        Send
      </button>
    </div>
  );

  const isEmpty = messages.length === 0 && !loading;

  return (
    <div className="flex flex-col h-full flex-1 min-h-0">
      {isEmpty ? (
        <form onSubmit={handleSubmit} className="flex-1 flex flex-col items-center justify-center gap-4 px-4 overflow-y-auto">
          <StarterPrompts onSelect={onSubmit} />
          {inputBar}
        </form>
      ) : (
        <>
          <div className="flex-1 overflow-y-auto px-4 py-6">
            <div className="max-w-3xl mx-auto">
              {messages.map((m, i) => <MessageBubble key={i} message={m} />)}
              {loading && <LoadingIndicator />}
              <div ref={bottomRef} />
            </div>
          </div>
          <form onSubmit={handleSubmit} className="border-t border-neutral-200 dark:border-neutral-800 p-4">
            {inputBar}
          </form>
        </>
      )}
    </div>
  );
}