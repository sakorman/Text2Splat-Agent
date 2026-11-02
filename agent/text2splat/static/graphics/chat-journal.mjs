const storageKey = "text2splat.pending-chat.v1";
const keyPattern = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/;

export function readPendingChat(storage) {
  const raw = storage.getItem(storageKey);
  if (!raw) return null;
  try {
    const record = JSON.parse(raw);
    const body = JSON.parse(record.body);
    if (!keyPattern.test(record.key) || typeof body.message !== "string" ||
        !body.message.trim() || body.message.length > 2000 ||
        (body.conversationId !== null && !/^[a-f0-9]{32}$/.test(body.conversationId)))
      throw new Error("Invalid pending chat");
    return {key: record.key, body: record.body};
  } catch {
    throw new Error("本地消息记录无法读取，请保留页面地址并检查浏览器存储。");
  }
}
export function preparePendingChat(storage, body) {
  const previous = readPendingChat(storage);
  if (previous?.body === body) return previous;
  if (previous) throw new Error("上一条消息尚未确认，请刷新恢复或重试原消息后再发送新内容。");
  const record = {key: crypto.randomUUID(), body};
  storage.setItem(storageKey, JSON.stringify(record));
  return record;
}
export function clearPendingChat(storage, key) {
  if (readPendingChat(storage)?.key === key) storage.removeItem(storageKey);
}
