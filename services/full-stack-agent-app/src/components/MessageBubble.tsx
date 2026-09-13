// services/full-stack-agent-app/src/components/MessageBubble.tsx
"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ThinkingLog from "./ThinkingLog";
import PartyChart from "./PartyChart";
import type { ChatMessage } from "@/lib/types";
import { formatDuration } from "@/lib/format";

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      <div
        className={`max-w-2xl rounded-2xl px-4 py-3 ${
          isUser
            ? "bg-black text-white"
            : "bg-neutral-100 text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100"
        }`}
      >
        {!isUser && message.thinking_log && message.thinking_log.length > 0 && (
          <ThinkingLog steps={message.thinking_log} />
        )}
        {!isUser && message.generation_time_seconds != null && (
          <p className="mb-2 text-xs text-neutral-400 dark:text-neutral-500">
            Answer generated in {formatDuration(message.generation_time_seconds)}
          </p>
        )}
        <div className={`prose prose-sm max-w-none ${isUser ? "prose-invert" : "dark:prose-invert"}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
        </div>
        {!isUser && message.chart_data && <PartyChart chartData={message.chart_data} />}
      </div>
    </div>
  );
}