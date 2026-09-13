// services/full-stack-agent-app/src/lib/agentClient.ts
import "server-only";

// Server-only module: reads AGENT_SERVICE_URL and APP_SHARED_SECRET from
// process.env and attaches the secret as the x-app-secret header, exactly
// like agent-app's _get_http_client() did in Streamlit. Because this file
// is only ever imported from Route Handlers (src/app/api/**/route.ts),
// the secret never reaches a client bundle.

const AGENT_SERVICE_URL = process.env.AGENT_SERVICE_URL as string;
const APP_SHARED_SECRET = process.env.APP_SHARED_SECRET as string;

// GET /health reliably wakes a sleeping Render free-tier instance; POST
// requests do not (same empirical finding _wake_agent_service() in the
// old app.py documented). Poll /health until it responds before retrying
// the real request.
async function wakeAgentService(maxWaitMs = 60000, pollIntervalMs = 3000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    try {
      const res = await fetch(`${AGENT_SERVICE_URL}/health`, {
        signal: AbortSignal.timeout(10000),
        cache: "no-store",
      });
      if (res.ok) return true;
    } catch {
      // ignore transport errors while polling
    }
    await new Promise((r) => setTimeout(r, pollIntervalMs));
  }
  return false;
}

async function requestWithRetry(path: string, init: RequestInit, retries = 3): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = await fetch(`${AGENT_SERVICE_URL}${path}`, {
        ...init,
        cache: "no-store",
        headers: {
          ...(init.headers || {}),
          "x-app-secret": APP_SHARED_SECRET,
          "content-type": "application/json",
        },
      });
      if ([502, 503, 504].includes(res.status)) {
        await wakeAgentService();
        continue;
      }
      if (!res.ok) {
        throw new Error(`agent-service returned ${res.status}`);
      }
      return res;
    } catch (err) {
      lastErr = err;
      await wakeAgentService();
    }
  }
  throw lastErr ?? new Error("agent-service did not respond after wake attempts");
}

export async function runQuery(query: string, threadId: string) {
  const res = await requestWithRetry("/query", {
    method: "POST",
    body: JSON.stringify({ query, thread_id: threadId }),
  });
  return res.json();
}

export async function getThreadMessages(threadId: string) {
  const res = await requestWithRetry(`/threads/${threadId}/messages`, { method: "GET" });
  return res.json();
}