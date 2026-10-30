// Keep an unresolved submission in this browser tab until its job ID is known.
const storageKey = "text2splat.pending-request.v1";
const keyPattern = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/;

export function readPendingRequest(storage) {
  const raw = storage.getItem(storageKey);
  if (!raw) return null;
  try {
    const record = JSON.parse(raw);
    const body = JSON.parse(record.body);
    if (
      !keyPattern.test(record.key) ||
      body.mode !== "THREE_D" ||
      typeof body.prompt !== "string" ||
      body.prompt.trim().length < 2 ||
      body.prompt.length > 2000
    )
      throw new Error("Invalid pending request");
    return { key: record.key, body: record.body };
  } catch {
    throw new Error(
      "本地提交记录无法读取。请保留当前页面地址，检查浏览器存储后再试。",
    );
  }
}

export function preparePendingRequest(storage, body) {
  const previous = readPendingRequest(storage);
  if (previous?.body === body) return previous;
  const request = { key: crypto.randomUUID(), body };
  // A storage failure must prevent submission, not silently remove replay protection.
  storage.setItem(storageKey, JSON.stringify(request));
  return request;
}

export function clearPendingRequest(storage, key) {
  if (readPendingRequest(storage)?.key === key) storage.removeItem(storageKey);
}
