import test from "node:test";
import assert from "node:assert/strict";
import {
  readPendingRequest,
  preparePendingRequest,
  clearPendingRequest,
} from "../src/main/resources/static/graphics/request-journal.mjs";

function storage() {
  const data = new Map();
  return {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
}
const body = JSON.stringify({ prompt: "一个青瓷花瓶", mode: "THREE_D" });

test("refresh recovers the exact body and key, and a retry reuses that key", () => {
  const tab = storage();
  const first = preparePendingRequest(tab, body);
  const restored = readPendingRequest(tab);
  assert.deepEqual(restored, first);
  assert.deepEqual(preparePendingRequest(tab, restored.body), first);
});
test("a different description starts a new request; late cleanup cannot erase it", () => {
  const tab = storage();
  const first = preparePendingRequest(tab, body);
  const second = preparePendingRequest(
    tab,
    JSON.stringify({ prompt: "一只木雕小鸟", mode: "THREE_D" }),
  );
  assert.notEqual(first.key, second.key);
  clearPendingRequest(tab, first.key);
  assert.deepEqual(readPendingRequest(tab), second);
  clearPendingRequest(tab, second.key);
  assert.equal(readPendingRequest(tab), null);
});
test("storage failure prevents creating an unjournaled submission", () => {
  const tab = storage();
  tab.setItem = () => {
    throw new Error("Storage blocked");
  };
  assert.throws(() => preparePendingRequest(tab, body), /Storage blocked/);
  assert.equal(readPendingRequest(tab), null);
});
test("a damaged journal is preserved instead of silently creating a fresh request", () => {
  const tab = storage();
  tab.setItem("text2splat.pending-request.v1", '{"key":"broken"}');
  assert.throws(() => readPendingRequest(tab), /本地提交记录无法读取/);
  assert.throws(() => preparePendingRequest(tab, body), /本地提交记录无法读取/);
  assert.equal(
    tab.getItem("text2splat.pending-request.v1"),
    '{"key":"broken"}',
  );
});
