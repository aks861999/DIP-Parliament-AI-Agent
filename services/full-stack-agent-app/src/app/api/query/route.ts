// services/full-stack-agent-app/src/app/api/query/route.ts
import { NextRequest, NextResponse } from "next/server";
import { runQuery } from "@/lib/agentClient";

// Thin proxy to agent-service's POST /query. The browser never talks to
// agent-service directly and never sees APP_SHARED_SECRET -- it only
// ever calls this same-origin route, which attaches the secret server-side.
export async function POST(req: NextRequest) {
  const body = await req.json();
  const { query, thread_id } = body ?? {};

  if (!query || !thread_id) {
    return NextResponse.json({ error: "query and thread_id are required" }, { status: 400 });
  }

  try {
    const result = await runQuery(query, thread_id);
    return NextResponse.json(result);
  } catch (err) {
    console.error("agent-service query failed", err);
    return NextResponse.json(
      { answer: "Sorry, the backend took too long to respond. Please try again.", thinking_log: [] },
      { status: 502 }
    );
  }
}
