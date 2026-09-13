// services/full-stack-agent-app/src/lib/format.ts
// Shared by LoadingIndicator (live-ticking, cosmetic) and MessageBubble
// (final, actually-measured generation time) so both render identically.
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}