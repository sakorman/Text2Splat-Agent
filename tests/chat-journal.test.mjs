import test from 'node:test';
import assert from 'node:assert/strict';
import {readPendingChat, preparePendingChat, clearPendingChat} from '../agent/text2splat/static/graphics/chat-journal.mjs';
const memory = () => { const data = new Map(); return {getItem:k=>data.get(k)??null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)}; };
const body = JSON.stringify({message:'你好', conversationId:null});
test('chat journal restores original message and idempotency key',()=>{
  const store=memory(), first=preparePendingChat(store,body);
  assert.deepEqual(readPendingChat(store),first);
  assert.deepEqual(preparePendingChat(store,body),first);
  clearPendingChat(store,'another-key');
  assert.deepEqual(readPendingChat(store),first);
  clearPendingChat(store,first.key);
  assert.equal(readPendingChat(store),null);
});
test('an unresolved message cannot be silently replaced by a different send',()=>{
  const store=memory(), first=preparePendingChat(store,body);
  assert.throws(()=>preparePendingChat(store,JSON.stringify({message:'生成花瓶',conversationId:null})),/尚未确认/);
  assert.deepEqual(readPendingChat(store),first);
});
test('corrupted storage and unavailable storage fail before a send',()=>{
  assert.throws(()=>readPendingChat({getItem:()=>'{'}),/无法读取/);
  assert.throws(()=>preparePendingChat({getItem:()=>null,setItem:()=>{throw new Error('unavailable');}},body),/unavailable/);
});
