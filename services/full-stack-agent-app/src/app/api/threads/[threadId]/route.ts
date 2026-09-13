// services/full-stack-agent-app/src/app/api/threads/[threadId]/route.ts
//
// Updated for Next.js 16: `params` is now ALWAYS a Promise, with no
// synchronous fallback (Next.js 15 allowed sync access with a warning;
// 16 removed that fallback entirely). Both handlers below now type
// `params` as a Promise and `await` it before use.
import { NextRequest, NextResponse } from "next/server";
import { getThreadMessages } from "@/lib/agentClient";
import { touchThread } from "@/lib/db";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ threadId: string }> }
) {
  const { threadId } = await params;
  try {
    const messages = await getThreadMessages(threadId);
    return NextResponse.json(messages);
  } catch (err) {
    console.error("failed to load thread messages", err);
    return NextResponse.json({ error: "could not load chat history" }, { status: 502 });
  }
}