const $ = (id) => document.getElementById(id);
const terminal = new Set([
  "READY",
  "CONCEPT_READY",
  "NEEDS_REVIEW",
  "FAILED",
  "CANCELLED",
]);
const stages = {
  QUEUED: "正在准备生成…",
  PLANNING: "正在理解你的描述…",
  REFERENCE: "正在生成物件图像…",
  MULTIVIEW: "正在生成和检查不同角度…",
  IMPORTING: "正在准备构建模型…",
  RECONSTRUCTING: "正在构建 3D 模型，可能需要一些时间。",
  VALIDATING: "正在检查生成的模型…",
};
let current, events, pendingRequest;
let submitting = false;
let restoring = false;

async function api(path, options) {
  const response = await fetch("/api/graphics" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      data.message || `请求失败（${response.status}），请稍后重试。`,
    );
  return data;
}
function showError(message) {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
}
function updateControls() {
  const running = current && !terminal.has(current.status);
  $("prompt").disabled = submitting || restoring || !!running;
  $("create-button").disabled = submitting || restoring || !!running;
  $("create-button").textContent = submitting
    ? "正在提交…"
    : restoring
      ? "正在恢复进度…"
      : running
        ? "正在生成…"
        : "生成 3D 模型";
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
    ? ""
    : failed
      ? failureMessage(job)
      : job.status === "CONCEPT_READY"
        ? "点击「生成 3D 模型」，用这段描述开始完整生成。"
        : job.status === "CANCELLING"
          ? "正在停止后续步骤；尚未确认停止时，可再次取消。"
          : job.status === "CANCELLED"
            ? "可以修改描述，再生成一次。"
            : stages[job.status] || "正在处理，请稍候…";
  $("progress-message").hidden = ready;
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
  events?.close();
  $("prompt").value = job.prompt;
  window.history.replaceState(null, "", `?job=${job.id}`);
  render(job);
  if (!terminal.has(job.status)) watch(job.id);
}
$("prompt").addEventListener("input", () => {
  $("prompt").setCustomValidity("");
  showError("");
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitting || restoring || (current && !terminal.has(current.status)))
    return;
  const prompt = $("prompt").value.trim();
  if (prompt.length < 2) {
    $("prompt").setCustomValidity("请至少输入两个字，描述你想生成的物件。");
    $("prompt").reportValidity();
    return;
  }
  showError("");
  submitting = true;
  updateControls();
  const body = JSON.stringify({ prompt, mode: "THREE_D" });
  // Retry an ambiguous submission with the same key; never start duplicate generation.
  if (!pendingRequest || pendingRequest.body !== body)
    pendingRequest = { body, key: crypto.randomUUID() };
  try {
    const job = await api("/jobs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": pendingRequest.key,
      },
      body,
    });
    pendingRequest = null;
    openJob(job);
  } catch (error) {
    showError(
      error instanceof TypeError
        ? "连接失败，请检查本地服务后重试。"
        : error.message,
    );
  } finally {
    submitting = false;
    updateControls();
  }
});
$("cancel-button").addEventListener("click", async () => {
  if (!current || terminal.has(current.status)) return;
  $("cancel-button").disabled = true;
  showError("");
  try {
    render(await api(`/jobs/${current.id}/cancel`, { method: "POST" }));
  } catch (error) {
    showError(error.message);
  } finally {
    $("cancel-button").disabled = false;
  }
});
const selected = new URLSearchParams(location.search).get("job");
if (selected && /^[a-f0-9]{32}$/.test(selected)) {
  restoring = true;
  updateControls();
  try {
    openJob(await api("/jobs/" + selected));
  } catch (error) {
    showError(error.message);
  } finally {
    restoring = false;
    updateControls();
  }
}
