// Android Back (the button, or the edge-swipe back gesture, which is easy to hit while switching apps) used to close the full-screen workout at once through
// Modal.onRequestClose, and that ended the voice session (ERROR_LEDGER E-008). The workout screen registers a handler here; Today's Modal asks it first.
// Pure TypeScript.
let handler: (() => boolean) | null = null;

/** The workout screen registers while it is open. Returns the unregister function. The handler returns true when it dealt with the Back press itself. */
export function setLiveBackHandler(h: () => boolean): () => void {
  handler = h;
  return () => { if (handler === h) handler = null; };
}

/** true = the workout screen handled Back (e.g. showed "Leave the workout?"); false = nobody did, the caller closes as before. */
export function handleLiveBack(): boolean {
  try { return !!handler?.(); } catch { return false; }
}
