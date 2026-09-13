// services/full-stack-agent-app/src/app/api/login/route.ts
import { NextRequest, NextResponse } from "next/server";

// Equivalent of agent-app's _check_password(): compares against a single
// shared APP_PASSWORD. On success, sets an httpOnly cookie whose value is
// APP_SHARED_SECRET -- the same secret already trusted by agent-service's
// x-app-secret check -- so no separate session-signing scheme is needed.
export async function POST(req: NextRequest) {
  const { password } = await req.json();
  const expectedPassword = process.env.APP_PASSWORD;
  const secret = process.env.APP_SHARED_SECRET;

  if (!expectedPassword || !secret) {
    return NextResponse.json({ error: "server not configured" }, { status: 500 });
  }
  if (password !== expectedPassword) {
    return NextResponse.json({ error: "incorrect password" }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set("dip_session", secret, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24, // 24 hours
  });
  return res;
}