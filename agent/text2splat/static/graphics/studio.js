import {
  readPendingRequest,
  clearPendingRequest,
} from "./request-journal.mjs";
import { readPendingChat, preparePendingChat, clearPendingChat } from "./chat-journal.mjs";
import { terminal, acceptsJobUpdate } from "./job-updates.mjs";

const $ = (id) => document.getElementById(id);
const stages = {
  QUEUED: "正在准备生成…",
  PLANNING: "正在理解你的描述…",
  REFERENCE: "正在生成物件图像…",
  MULTIVIEW: "正在生成和检查不同角度…",
  IMPORTING: "正在准备构建模型…",
  RECONSTRUCTING: "正在构建 3D 模型，可能需要一些时间。",
  VALIDATING: "正在检查生成的模型…",
};
let current, events, pendingRequest, pendingChat;
let conversationId = null, turns = [], pendingTurn = null, chatPoll;
let submitting = false;
let restoring = false;
const pendingCancellations = new Set();

async function api(path, options) {
  const response = await fetch("/api/graphics" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      data.message || `请求失败（${response.status}），请稍后重试。`,
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
function showError(message) {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
}
function updateControls() {
  $("prompt").disabled = submitting || restoring || !!pendingTurn;
  $("create-button").disabled = submitting || restoring || !!pendingTurn;
  $("cancel-button").disabled = pendingCancellations.has(current?.id);
  $("create-button").textContent = submitting ? "正在发送…" : restoring ? "正在恢复…" : pendingTurn ? "正在回复…" : "发送";
}
function safeLocalUrl(value) {
  if (typeof value !== "string" || !value) return null;
  try {
    const url = new URL(value, location.origin);
    return url.protocol === "http:" &&
      ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      ? url.href
      : null;
  } catch {
    return null;
  }
}
function addLink(label, href) {
  const url = safeLocalUrl(href);
  if (!url) return;
  const link = document.createElement("a");
  link.textContent = label;
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  $("result-actions").append(link);
}
function failureMessage(job) {
  if (job.message?.includes("ZHIPU_API_KEY"))
    return "图片生成服务尚未配置。请在本地 .env 中填写 ZHIPU_API_KEY，重启服务后再试。";
  return job.message || "这次没有生成可用的 3D 模型，请调整描述后再试。";
}
function render(job) {
  current = job;
  const done = terminal.has(job.status);
  const failed = ["FAILED", "NEEDS_REVIEW"].includes(job.status);
  const ready = job.status === "READY";
  $("progress-panel").hidden = false;
  $("progress-panel").dataset.state = ready
    ? "done"
    : failed
      ? "error"
      : done
        ? "stopped"
        : "running";
  $("progress-title").textContent = ready
    ? "3D 模型已生成"
    : failed
      ? "这次生成未完成"
      : job.status === "CANCELLED"
        ? "已取消生成"
        : job.status === "CANCELLING"
          ? "正在取消生成"
          : job.status === "CONCEPT_READY"
            ? "这个旧任务只生成了图片"
            : "正在生成 3D 模型";
  $("progress-message").textContent = ready
    ? "模型预览在下方，可滚动查看。"
    : failed
      ? failureMessage(job)
      : job.status === "CONCEPT_READY"
        ? "这是此前的概念图片任务。如需 3D 模型，请在对话中提出生成要求。"
        : job.status === "CANCELLING"
          ? "正在停止后续步骤；尚未确认停止时，可再次取消。"
          : job.status === "CANCELLED"
            ? "可以继续聊天，准备好了再提出新的生成要求。"
            : job.connectionRetry > 0
              ? "进度连接暂时中断，正在恢复；无需重新生成。"
              : stages[job.status] || "正在处理，请稍候…";
  $("progress-message").hidden = false;
  $("cancel-button").hidden = done;
  $("cancel-button").textContent =
    job.status === "CANCELLING" ? "再次取消" : "取消生成";

  const viewerUrl = ready ? safeLocalUrl(job.result?.viewer_url) : null;
  const model = ready
    ? job.result?.files?.find((file) => file.name === "gaussians.ply")
    : null;
  $("result").hidden = !ready;
  $("viewer").hidden = !viewerUrl;
  if (viewerUrl && $("viewer").src !== viewerUrl) $("viewer").src = viewerUrl;
  if (!viewerUrl) $("viewer").removeAttribute("src");
  $("result-actions").replaceChildren();
  if (model) addLink("下载模型", model.url);
  if (viewerUrl) addLink("打开完整预览", viewerUrl);
  $("result").querySelector("p").textContent = viewerUrl
    ? "拖动旋转，滚动缩放"
    : "预览暂不可用，可下载模型查看。";
  if (done) {
    events?.close();
    $("stream-note").hidden = true;
  }
  updateControls();
}
function watch(id) {
  events?.close();
  const stream = new EventSource(`/api/graphics/jobs/${id}/events`);
  events = stream;
  stream.addEventListener("progress", (event) => {
    if (current?.id !== id) return;
    try {
      const job = JSON.parse(event.data);
      if (!acceptsJobUpdate(current, job, id)) return;
      $("stream-note").hidden = true;
      render(job);
    } catch {
      $("stream-note").hidden = false;
      $("stream-note").textContent =
        "进度暂时不可用，请刷新页面恢复。刷新不会重复生成。";
    }
  });
  stream.onerror = () => {
    if (current?.id === id && !terminal.has(current.status)) {
      $("stream-note").hidden = false;
      $("stream-note").textContent =
        "进度连接中断，正在重连。刷新页面也可恢复进度。";
    }
  };
}
function openJob(job) {
  if (current?.id === job.id) {
    if (acceptsJobUpdate(current, job, job.id)) render(job);
    return;
  }
  events?.close();
  render(job);
  if (!terminal.has(job.status)) watch(job.id);
}
function renderConversation() {
  const log = $("conversation");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const nodes = [];
  for (const turn of turns) {
    for (const [role, text] of [["user", turn.message], ["assistant", turn.assistant || "正在回复…"]]) {
      const row = document.createElement("div");
      row.className = "chat-message " + role;
      if (role === "assistant" && turn.status === "FAILED") row.classList.add("chat-error");
      const label = document.createElement("span");
      label.className = "chat-label";
      label.textContent = role === "user" ? "你" : "造物方舟";
      const content = document.createElement("p");
      content.textContent = text;
      row.append(label, content);
      nodes.push(row);
    }
  }
  log.replaceChildren(...nodes);
  log.hidden = !turns.length;
  document.querySelector(".workspace").classList.toggle("has-conversation", !!turns.length);
  if (atBottom) log.scrollTop = log.scrollHeight;
}
async function openConversation(cid) {
  const data = await api("/chats/" + cid);
  conversationId = cid;
  turns = data.turns;
  window.history.replaceState(null, "", `?chat=${cid}`);
  renderConversation();
  if (data.latestJob) openJob(data.latestJob);
  const waiting = turns.find(t => !["COMPLETED", "FAILED"].includes(t.status));
  clearTimeout(chatPoll);
  pendingTurn = waiting?.id || null;
  if (waiting) pollTurn(waiting.id);
  updateControls();
}
function pollTurn(id) {
  clearTimeout(chatPoll);
  chatPoll = setTimeout(async () => {
    if (pendingTurn !== id) return;
    try {
      const turn = await api("/chat/turns/" + id);
      if (["COMPLETED", "FAILED"].includes(turn.status)) {
        await openConversation(turn.conversationId);
        showError("");
        return;
      }
    } catch (error) {
      showError("回复连接暂时中断，正在重连。刷新可恢复，不会重复发送。");
    }
    pollTurn(id);
  }, 800);
}
$("prompt").addEventListener("input", () => {
  $("prompt").setCustomValidity("");
  showError("");
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting || restoring || pendingTurn) return;
  const message = $("prompt").value.trim();
  if (!message) {
    $("prompt").setCustomValidity("请输入消息。");
    $("prompt").reportValidity();
    return;
  }
  showError("");
  submitting = true;
  updateControls();
  const body = JSON.stringify({ message, conversationId });
  try {
    pendingChat = preparePendingChat(sessionStorage, body);
    window.history.replaceState(null, "", `?chat_request=${pendingChat.key}`);
    const turn = await api("/chat", {
      method: "POST", headers: {"Content-Type": "application/json", "Idempotency-Key": pendingChat.key}, body,
    });
    // The server accepted this turn; its conversation URL can now recover it read-only.
    window.history.replaceState(null, "", `?chat=${turn.conversationId}`);
    clearPendingChat(sessionStorage, pendingChat.key);
    pendingChat = null;
    $("prompt").value = "";
    conversationId = turn.conversationId;
    pendingTurn = turn.id;
    if (!turns.some(t => t.id === turn.id)) turns.push(turn);
    renderConversation();
    pollTurn(turn.id);
    await openConversation(turn.conversationId);
  } catch (error) {
    showError(error instanceof TypeError ? "连接失败，请刷新恢复原消息或重试。" : error.message);
  } finally {
    submitting = false;
    updateControls();
  }
});
$("cancel-button").addEventListener("click", async () => {
  if (!current || terminal.has(current.status) || pendingCancellations.has(current.id)) return;
  const id = current.id;
  pendingCancellations.add(id);
  updateControls();
  showError("");
  try {
    const job = await api(`/jobs/${id}/cancel`, { method: "POST" });
    if (acceptsJobUpdate(current, job, id)) render(job);
  } catch (error) {
    if (current?.id === id && !terminal.has(current.status)) showError(error.message);
  } finally {
    pendingCancellations.delete(id);
    updateControls();
  }
});
async function restoreSubmission() {
  const query = new URLSearchParams(location.search);
  restoring = true;
  updateControls();
  try {
    pendingChat = readPendingChat(sessionStorage);
    const key = query.get("chat_request") || pendingChat?.key;
    if (key) {
      if (!/^[a-zA-Z0-9._:-]{1,160}$/.test(key)) throw new Error("消息标识无效");
      if (pendingChat?.key === key) {
        const body = JSON.parse(pendingChat.body);
        $("prompt").value = body.message;
        conversationId = body.conversationId;
      }
      const turn = await api("/chat/requests/" + key);
      clearPendingChat(sessionStorage, key);
      pendingChat = null;
      $("prompt").value = "";
      await openConversation(turn.conversationId);
      return;
    }
    const cid = query.get("chat");
    if (cid) {
      if (!/^[a-f0-9]{32}$/.test(cid)) throw new Error("对话标识无效");
      await openConversation(cid);
      return;
    }
    // Preserve old job links and unresolved pre-chat submissions without replaying them.
    pendingRequest = readPendingRequest(sessionStorage);
    const selected = query.get("job");
    const requestKey = query.get("request") || pendingRequest?.key;
    if (selected) {
      const job = await api("/jobs/" + selected);
      openJob(job);
      if (pendingRequest?.key === job.requestKey) clearPendingRequest(sessionStorage, pendingRequest.key);
    } else if (requestKey) {
      const job = await api("/requests/" + requestKey);
      window.history.replaceState(null, "", `?job=${job.id}`);
      openJob(job);
      clearPendingRequest(sessionStorage, requestKey);
    }
  } catch (error) {
    showError(error.status === 404 && pendingChat ? "上次消息尚未确认，请重试原消息，会沿用原提交标识。" : error.message);
  } finally {
    restoring = false;
    updateControls();
  }
}
await restoreSubmission();
