// services/full-stack-agent-app/src/components/LoadingIndicator.tsx
"use client";

import { useEffect, useState } from "react";
import { formatDuration } from "@/lib/format";

// COSMETIC ONLY -- these phase labels cycle on a fixed timer and do NOT
// reflect what agent-service is actually doing right now. The backend
// returns everything in one blocking response, so there's no real
// progress signal to show. If real step-by-step status is ever added
// later (via an SSE endpoint), this component should be replaced, not
// extended, so it's never confused with a real signal again.
const PHASES = [
  "🧭 Classifying your question...",
  "🔧 Retrieving parliamentary data...",
  "🔍 Checking completeness...",
  "✅ Verifying the answer...",
  "📝 Writing the response...",
];

export default function LoadingIndicator() {
  const [phaseIndex, setPhaseIndex] = useState(0);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const phaseTimer = setInterval(() => {
      setPhaseIndex((i) => (i + 1) % PHASES.length);
    }, 2500);
    const secondTimer = setInterval(() => {
      setElapsed((s) => s + 1);
    }, 1000);
    return () => {
      clearInterval(phaseTimer);
      clearInterval(secondTimer);
    };
  }, []);

  return (
    <div className="flex justify-start mb-4">
        <div className="rounded-2xl bg-neutral-100 dark:bg-neutral-800 px-4 py-3 text-sm text-neutral-500 dark:text-neutral-400">
        {PHASES[phaseIndex]}{" "}
        <span className="text-neutral-400 dark:text-neutral-500">(thinking for {formatDuration(elapsed)})</span>
        </div>
    </div>
  );
}