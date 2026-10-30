const $ = (id) => document.getElementById(id);
const terminal = new Set([
  "READY",
  "CONCEPT_READY",
  "NEEDS_REVIEW",
  "FAILED",
  "CANCELLED",
]);
const names = {
  QUEUED: "等待开始",
  PLANNING: "整理设计",
  REFERENCE: "生成参考图",
  MULTIVIEW: "生成候选视图",
  IMPORTING: "准备重建",
  RECONSTRUCTING: "本地重建",
  VALIDATING: "检查产物",
  READY: "3D 已完成",
  CONCEPT_READY: "概念图已完成",
  NEEDS_REVIEW: "需要调整",
  FAILED: "未完成",
  CANCELLING: "正在取消",
  CANCELLED: "已取消",
};
let current,
  events,
  health,
  pendingRequest,
  parentForNext,
  history = [];
const api = async (path, options) => {
  const response = await fetch("/api/graphics" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(data.message || `请求失败（${response.status}）`);
  return data;
};
const showError = (message) => {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
};
function safeLocalUrl(value, asset = false) {
  try {
    const url = new URL(value, location.origin);
    if (asset)
      return url.origin === location.origin &&
        url.pathname.startsWith("/api/graphics/assets/")
        ? url.href
        : null;
    return url.protocol === "http:" &&
      ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      ? url.href
      : null;
  } catch {
    return null;
  }
}
function addLink(container, label, href, asset = false) {
  const url = safeLocalUrl(href, asset);
  if (!url) return;
  const link = document.createElement("a");
  link.textContent = label;
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  container.append(link);
}
function render(job) {
  current = job;
  const done = terminal.has(job.status);
  $("work-title").textContent =
    job.spec?.title ||
    (job.prompt.length > 28 ? job.prompt.slice(0, 28) + "…" : job.prompt);
  $("work-title").title = job.prompt;
  $("work-kind").textContent =
    job.mode === "THREE_D" ? "3D 生成尝试" : "概念设计";
  $("status").textContent = names[job.status] || job.status;
  $("status").className =
    "status" +
    (["READY", "CONCEPT_READY"].includes(job.status)
      ? " success"
      : ["FAILED", "NEEDS_REVIEW"].includes(job.status)
        ? " error"
        : "");
  $("progress-panel").hidden = false;
  $("progress-title").textContent = names[job.status] || "正在处理";
  $("progress-message").textContent = job.message || "";
  $("cancel-button").hidden = done;
  $("cancel-button").textContent =
    job.status === "CANCELLING" ? "重试取消" : "取消任务";
  $("display-stage").classList.toggle("loading", !done);
  const refUrl = safeLocalUrl(job.referenceAssets?.[0]?.url, true);
  $("reference-image").hidden = !refUrl;
  if (refUrl && $("reference-image").src !== refUrl)
    $("reference-image").src = refUrl;
  const viewerUrl =
    job.status === "READY" ? safeLocalUrl(job.result?.viewer_url) : null;
  $("viewer").hidden = !viewerUrl;
  if (viewerUrl && $("viewer").src !== viewerUrl) $("viewer").src = viewerUrl;
  if (!viewerUrl) $("viewer").removeAttribute("src");
  if (viewerUrl) $("reference-image").hidden = true;
  $("empty-state").hidden = !!refUrl || !!viewerUrl;
  $("stage-caption").hidden = !refUrl || !!viewerUrl;
  const stage =
    {
      QUEUED: 0,
      PLANNING: 0,
      REFERENCE: 1,
      MULTIVIEW: 2,
      IMPORTING: 2,
      RECONSTRUCTING: 3,
      VALIDATING: 4,
      READY: 5,
      CONCEPT_READY: 2,
    }[job.status] ?? -1;
  $("steps").hidden = done && !["READY", "CONCEPT_READY"].includes(job.status);
  [...$("steps").children].forEach((li, index) => {
    li.hidden = job.mode === "CONCEPT" && index > 1;
    li.className = index < stage ? "done" : index === stage ? "current" : "";
  });
  const assets = job.viewAssets || [];
  $("contact-sheet").hidden = !assets.length;
  $("view-count").textContent = `${assets.length} / 12`;
  const signature = assets.map((a) => a.url).join("|");
  if ($("view-grid").dataset.signature !== signature) {
    $("view-grid").dataset.signature = signature;
    $("view-grid").replaceChildren();
    assets.forEach((asset, i) => {
      const url = safeLocalUrl(asset.url, true);
      if (!url) return;
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      const img = document.createElement("img");
      img.src = url;
      img.alt = `候选视图 ${i + 1}`;
      img.loading = "lazy";
      const caption = document.createElement("span");
      caption.textContent = `视图 ${i + 1}`;
      a.append(img, caption);
      $("view-grid").append(a);
    });
  }
  const actions = $("result-actions");
  actions.replaceChildren();
  actions.hidden = !done;
  if (done) {
    if (viewerUrl) addLink(actions, "打开 3D 展厅", viewerUrl);
    if (refUrl) addLink(actions, "查看参考图", refUrl, true);
    for (const file of job.result?.files || [])
      addLink(actions, `下载 ${file.name}`, file.url);
    const revise = document.createElement("button");
    revise.type = "button";
    revise.textContent = "修改描述，创建新版本";
    revise.addEventListener("click", () => {
      parentForNext = job.id;
      $("prompt").value = job.prompt;
      $("prompt").focus();
      $("prompt").scrollIntoView({ block: "center", behavior: "smooth" });
    });
    actions.append(revise);
  }
  if (done) {
    events?.close();
    $("stream-note").hidden = true;
  }
}
function watch(id) {
  events?.close();
  events = new EventSource(`/api/graphics/jobs/${id}/events`);
  events.addEventListener("progress", (event) => {
    if (current?.id !== id) return;
    try {
      const job = JSON.parse(event.data);
      $("stream-note").hidden = true;
      render(job);
      if (terminal.has(job.status)) loadHistory();
    } catch {
      $("stream-note").hidden = false;
      $("stream-note").textContent = "进度解析失败，请刷新作品。";
    }
  });
  events.onerror = () => {
    if (current && !terminal.has(current.status)) {
      $("stream-note").hidden = false;
      $("stream-note").textContent =
        "进度连接已断开，正在重连。刷新页面不会重复生成。";
    }
  };
}
async function select(id) {
  try {
    events?.close();
    const job = await api("/jobs/" + id);
    render(job);
    localStorage.setItem("graphics.current", id);
    window.history.replaceState(null, "", `?job=${id}`);
    if (!terminal.has(job.status)) watch(id);
    renderHistory();
  } catch (error) {
    showError(error.message);
  }
}
function renderHistory() {
  const list = $("history-list");
  list.replaceChildren();
  if (!history.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "还没有作品，从上面的描述开始。";
    list.append(p);
    return;
  }
  for (const job of history) {
    const button = document.createElement("button");
    button.type = "button";
    button.className =
      "history-item" + (current?.id === job.id ? " active" : "");
    const url = safeLocalUrl(job.referenceAssets?.[0]?.url, true);
    if (url) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      button.append(img);
    }
    const copy = document.createElement("span");
    copy.className = "history-copy";
    const title = document.createElement("strong");
    title.textContent = job.spec?.title || job.prompt;
    const status = document.createElement("small");
    status.textContent = `${names[job.status] || job.status}　${new Date(job.createdAt).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
    copy.append(title, status);
    button.append(copy);
    button.addEventListener("click", () => select(job.id));
    list.append(button);
  }
}
async function loadHistory() {
  try {
    history = await api("/jobs");
    renderHistory();
  } catch (error) {
    showError(error.message);
  }
}
async function loadHealth() {
  $("refresh-health").disabled = true;
  try {
    health = await api("/health");
    const creative = health.creative || {},
      scan = health.webscan || {};
    $("connection-text").textContent = creative.configured
      ? scan.brush_available
        ? "生成与重建已就绪"
        : "图片生成已就绪"
      : "需要配置生成服务";
    $("refresh-health").classList.toggle("ready", !!creative.configured);
    const details = $("service-details");
    details.replaceChildren();
    for (const text of [
      creative.configured
        ? `图片模型：${creative.image_model}`
        : creative.message || "尚未设置 ZHIPU_API_KEY",
      scan.brush_available
        ? "本地 Brush 已就绪"
        : scan.message || "web-scan / Brush 尚未就绪",
      creative.image_provider === "seedream"
        ? "当前选择 Seedream，图片生成会按供应商规则计费。"
        : "当前选择 CogView 免费模型，视图生成属于实验流程。",
    ]) {
      const p = document.createElement("p");
      p.textContent = text;
      details.append(p);
    }
    updateMode();
  } catch (error) {
    $("connection-text").textContent = "本地 Agent 未连接";
    showError(error.message);
  } finally {
    $("refresh-health").disabled = false;
  }
}
function updateMode() {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  $("create-button").firstChild.textContent =
    mode === "CONCEPT" ? "生成概念图 " : "生成视图并尝试重建 ";
  $("mode-note").textContent =
    mode === "THREE_D"
      ? "候选视图可能不一致；检查或重建失败时保留图片供调整。"
      : health?.creative?.image_provider === "seedream"
        ? "当前使用 Seedream，图片生成会计费。"
        : "默认使用免费模型。实际额度以供应商账号为准。";
}
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  $("create-button").disabled = true;
  const body = {
    prompt: $("prompt").value.trim(),
    mode: document.querySelector('input[name="mode"]:checked').value,
  };
  if (parentForNext) body.parentJobId = parentForNext;
  const serialized = JSON.stringify(body);
  // Reuse the key after an ambiguous network failure; changed input gets a new version.
  if (!pendingRequest || pendingRequest.body !== serialized)
    pendingRequest = { body: serialized, key: crypto.randomUUID() };
  try {
    const job = await api("/jobs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": pendingRequest.key,
      },
      body: serialized,
    });
    pendingRequest = null;
    parentForNext = null;
    await select(job.id);
    await loadHistory();
  } catch (error) {
    showError(error.message);
  } finally {
    $("create-button").disabled = false;
  }
});
$("cancel-button").addEventListener("click", async () => {
  if (!current) return;
  $("cancel-button").disabled = true;
  try {
    render(await api(`/jobs/${current.id}/cancel`, { method: "POST" }));
  } catch (error) {
    showError(error.message);
  } finally {
    $("cancel-button").disabled = false;
  }
});
document.querySelectorAll("[data-example]").forEach((button) =>
  button.addEventListener("click", () => {
    parentForNext = null;
    $("prompt").value = button.dataset.example;
    $("prompt").focus();
  }),
);
document
  .querySelectorAll('input[name="mode"]')
  .forEach((radio) => radio.addEventListener("change", updateMode));
$("refresh-health").addEventListener("click", loadHealth);
$("refresh-history").addEventListener("click", loadHistory);
await loadHistory();
const selected =
  new URLSearchParams(location.search).get("job") ||
  localStorage.getItem("graphics.current");
if (selected && /^[a-f0-9]{32}$/.test(selected)) await select(selected);
loadHealth();
