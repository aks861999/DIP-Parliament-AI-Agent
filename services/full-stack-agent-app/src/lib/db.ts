// services/full-stack-agent-app/src/lib/db.ts
import "server-only";
import { Pool } from "pg";

// Same chat_threads table agent-app/db_utils.py already created and read
// from -- this service shares the identical DATABASE_URL, so no schema
// change or data migration is needed. This table is ONLY ever used for
// the thread list/titles in the sidebar; the actual message content
// (chat_turns) is owned exclusively by agent-service/chat_store.py and
// is only ever read here via agentClient.getThreadMessages(), never
// written to directly.

let pool: Pool | undefined;

function getPool(): Pool {
  if (!pool) {
    pool = new Pool({ connectionString: process.env.DATABASE_URL });
  }
  return pool;
}

export async function initDb(): Promise<void> {
  await getPool().query(`
    CREATE TABLE IF NOT EXISTS chat_threads (
      thread_id UUID PRIMARY KEY,
      title TEXT,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL
    )
  `);
}

export async function createThread(threadId: string, title: string): Promise<void> {
  await initDb();
  const now = new Date();
  await getPool().query(
    `INSERT INTO chat_threads (thread_id, title, created_at, updated_at)
     VALUES ($1, $2, $3, $4)
     ON CONFLICT (thread_id) DO NOTHING`,
    [threadId, title.slice(0, 80), now, now]
  );
}

export async function touchThread(threadId: string): Promise<void> {
  await getPool().query(`UPDATE chat_threads SET updated_at = $1 WHERE thread_id = $2`, [
    new Date(),
    threadId,
  ]);
}

export interface ChatThreadRow {
  thread_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export async function listThreads(): Promise<ChatThreadRow[]> {
  await initDb();
  const { rows } = await getPool().query(`SELECT * FROM chat_threads ORDER BY updated_at DESC`);
  return rows;
}