// services/full-stack-agent-app/src/app/api/threads/route.ts
import { NextRequest, NextResponse } from "next/server";
import { createThread, listThreads } from "@/lib/db";

// GET: sidebar's thread list, equivalent of db_utils.list_threads().
export async function GET() {
  const threads = await listThreads();
  return NextResponse.json(threads);
}

// POST: called once per NEW thread, right before its first message is
// sent -- equivalent of the old Streamlit app's
// `db_utils.create_thread(thread_id, title=query)` on first submission.
export async function POST(req: NextRequest) {
  const { thread_id, title } = await req.json();
  if (!thread_id || !title) {
    return NextResponse.json({ error: "thread_id and title are required" }, { status: 400 });
  }
  await createThread(thread_id, title);
  return NextResponse.json({ ok: true });
}
