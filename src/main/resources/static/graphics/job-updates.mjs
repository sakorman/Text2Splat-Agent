export const terminal = new Set([
  "READY",
  "CONCEPT_READY",
  "NEEDS_REVIEW",
  "FAILED",
  "CANCELLED",
]);

// HTTP cancellation responses and SSE progress can arrive in either order.
export function acceptsJobUpdate(current, incoming, expectedId) {
  if (
    !current ||
    current.id !== expectedId ||
    incoming?.id !== expectedId ||
    terminal.has(current.status)
  )
    return false;
  return (
    current.status !== "CANCELLING" ||
    incoming.status === "CANCELLING" ||
    terminal.has(incoming.status)
  );
}
