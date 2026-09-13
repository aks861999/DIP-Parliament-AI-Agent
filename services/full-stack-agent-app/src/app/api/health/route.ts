// services/full-stack-agent-app/src/app/api/health/route.ts
import { NextResponse } from "next/server";

// Used by the Dockerfile's HEALTHCHECK and, if you add one, Render's
// health check path -- deliberately public (listed in middleware.ts's
// PUBLIC_PATHS) so an orchestrator can probe it without a session cookie.
export async function GET() {
  return NextResponse.json({ status: "ok" });
}