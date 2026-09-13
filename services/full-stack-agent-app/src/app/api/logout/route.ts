// services/full-stack-agent-app/src/app/api/logout/route.ts
import { NextResponse } from "next/server";

export async function POST() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set("dip_session", "", { httpOnly: true, path: "/", maxAge: 0 });
  return res;
}