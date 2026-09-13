// services/full-stack-agent-app/src/proxy.ts
//
// MOVED from project root: Next.js only discovers proxy.ts (and, before
// it, middleware.ts) inside src/ when the project uses a src/ directory
// layout -- a root-level proxy.ts is silently never picked up, which is
// why no redirect to /login was happening at all. Delete the old
// root-level proxy.ts once this file is in place; only one should exist.
import { NextRequest, NextResponse } from "next/server";

const PUBLIC_PATHS = ["/login", "/api/login", "/api/health"];

export function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  const session = req.cookies.get("dip_session")?.value;
  const expected = process.env.APP_SHARED_SECRET;

  if (!expected || !session || session !== expected) {
    const loginUrl = new URL("/login", req.url);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};