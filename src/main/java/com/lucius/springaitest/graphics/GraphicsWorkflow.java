package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

@Service
@Profile("graphics")
public class GraphicsWorkflow {
    private final GraphicsJobStore store;
    private final GraphicsTools tools;
    private final GraphicsProperties properties;
    private final ObjectMapper mapper;
    private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> new Thread(r, "graphics-workflow"));
    private static class Cancelled extends RuntimeException {}
    private static class ReviewRequired extends RuntimeException {
        ReviewRequired(String message) { super(message); }
    }

    public GraphicsWorkflow(GraphicsJobStore store, GraphicsTools tools, GraphicsProperties properties, ObjectMapper mapper) {
        this.store = store; this.tools = tools; this.properties = properties; this.mapper = mapper;
    }

    @PostConstruct
    void recover() {
        for (ObjectNode old : store.list()) {
            if (!GraphicsJobStore.terminal(old)) worker.submit(() -> {
                String id = old.path("id").asText();
                boolean cleaned = cleanup(id);
                store.update(id, j -> {
                    j.put("status", cleaned ? (j.path("cancelRequested").asBoolean() ? "CANCELLED" : "FAILED") : "CANCELLING");
                    j.put("message", cleaned ? "服务重启，原任务已中断。可创建新版本；不会自动重发生成请求。"
                            : "服务重启，尚未确认旧任务停止。恢复 MCP 服务后点击取消重试。");
                });
            });
        }
    }

    public ObjectNode submit(String prompt, String mode, String parentId, String key) {
        var creation = store.create(prompt, mode, parentId, key);
        if (creation.created()) worker.submit(() -> run(creation.job().path("id").asText()));
        return creation.job();
    }

    public ObjectNode cancel(String id) {
        var job = store.update(id, j -> j.put("cancelRequested", true).put("status", "CANCELLING").put("message", "正在停止后续步骤并取消本地重建"));
        if (!GraphicsJobStore.terminal(job)) worker.submit(() -> finishCancellation(id));
        return job;
    }

    public ObjectNode health() {
        var result = mapper.createObjectNode();
        for (String name : new String[]{"creative", "webscan"}) {
            try { result.set(name, tools.call(name + "_health", Map.of())); }
            catch (RuntimeException e) { result.set(name, mapper.createObjectNode().put("available", false).put("message", e.getMessage())); }
        }
        return result;
    }

    private void run(String id) {
        Instant deadline = Instant.now().plus(properties.getJobTimeout());
        try {
            checkpoint(id);
            var job = store.get(id);
            if ("THREE_D".equals(job.path("mode").asText())) {
                var health = tools.call("webscan_health", Map.of());
                if (!health.path("available").asBoolean() || !health.path("brush_available").asBoolean())
                    throw new IllegalStateException("请先启动本地 web-scan 并安装 Brush，再尝试 3D 重建");
            }
            var provider = tools.call("creative_health", Map.of());
            if (!provider.path("configured").asBoolean()) throw new IllegalStateException("请在本地 .env 配置 ZHIPU_API_KEY 并重启 MCP 服务");
            stage(id, "PLANNING", "planning", "正在整理物件设计");
            store.update(id, j -> j.set("provider", provider));
            request(id, "referenceRequest", id + ":reference");
            var started = tools.call("creative_generate_reference", Map.of("prompt", job.path("prompt").asText(), "request_id", id + ":reference"));
            store.update(id, j -> j.put("creativeJobId", started.path("id").asText()));
            JsonNode reference = waitCreative(id, started.path("id").asText(), false, deadline);
            String referenceId = required(reference, "asset_set_id");
            store.update(id, j -> { j.put("referenceSetId", referenceId); j.set("spec", reference.path("spec")); });
            checkpoint(id);
            if ("CONCEPT".equals(job.path("mode").asText())) {
                stage(id, "CONCEPT_READY", "complete", "概念图已完成。可以修改描述，或创建一个 3D 尝试版本。");
                return;
            }
            stage(id, "MULTIVIEW", "multiview", "正在生成候选视图");
            request(id, "viewsRequest", id + ":views");
            var multi = tools.call("creative_generate_multiview", Map.of("reference_id", referenceId, "request_id", id + ":views"));
            store.update(id, j -> j.put("creativeJobId", multi.path("id").asText()));
            JsonNode views = waitCreative(id, multi.path("id").asText(), true, deadline);
            String viewsId = required(views, "asset_set_id");
            store.update(id, j -> j.put("viewsSetId", viewsId));
            stage(id, "IMPORTING", "importing", "正在导入本地重建服务");
            var imported = tools.call("webscan_import_generated_views", Map.of("asset_set_id", viewsId));
            String sid = required(imported, "session_id");
            store.update(id, j -> j.put("sessionId", sid));
            checkpoint(id);
            stage(id, "RECONSTRUCTING", "queued", "等待本地重建");
            request(id, "trainRequest", id + ":train");
            var train = tools.call("webscan_start_reconstruction", Map.of("session_id", sid, "request_id", id + ":train"));
            String jid = required(train, "job_id");
            store.update(id, j -> j.put("reconstructionId", jid));
            while (true) {
                checkpoint(id);
                checkDeadline(deadline);
                JsonNode status = tools.call("webscan_get_status", Map.of("job_id", jid));
                store.update(id, j -> {
                    j.put("stage", status.path("stage").asText()); j.put("message", status.path("message").asText());
                    j.set("reconstruction", status);
                });
                String state = status.path("status").asText();
                if ("succeeded".equals(state)) break;
                if ("failed".equals(state)) throw new ReviewRequired("本地重建失败：" + status.path("message").asText("视图几何不一致"));
                if ("cancelled".equals(state)) throw new Cancelled();
                pause();
            }
            checkpoint(id);
            stage(id, "VALIDATING", "export", "正在检查 3D 产物");
            var result = tools.call("webscan_get_result", Map.of("job_id", jid));
            checkpoint(id);
            store.update(id, j -> { j.set("result", result); j.put("status", "READY").put("stage", "complete").put("message", "3D 展品已生成，可旋转查看并下载"); });
        } catch (Cancelled e) {
            finishCancellation(id);
        } catch (RuntimeException e) {
            if (store.get(id).path("cancelRequested").asBoolean()) finishCancellation(id);
            else {
                boolean cleaned = cleanup(id);
                store.update(id, j -> j.put("status", cleaned ? (e instanceof ReviewRequired ? "NEEDS_REVIEW" : "FAILED") : "CANCELLING")
                        .put("message", cleaned ? safeMessage(e) : safeMessage(e) + "；尚未确认下游任务停止，恢复服务后点击取消重试。"));
            }
        }
    }

    private JsonNode waitCreative(String id, String childId, boolean views, Instant deadline) {
        while (true) {
            checkpoint(id);
            checkDeadline(deadline);
            JsonNode state = tools.call("creative_get_job", Map.of("job_id", childId));
            store.update(id, j -> {
                String phase = state.path("stage").asText();
                j.put("stage", phase).put("message", state.path("message").asText());
                j.put("status", views ? "MULTIVIEW" : "planning".equals(phase) ? "PLANNING" : "REFERENCE");
                if (state.has("assets")) j.set(views ? "viewAssets" : "referenceAssets", state.get("assets"));
                if (state.hasNonNull("review")) j.set("review", state.get("review"));
            });
            switch (state.path("status").asText()) {
                case "succeeded": return state;
                case "failed": throw new IllegalStateException(state.path("message").asText());
                case "needs_review": throw new ReviewRequired(state.path("message").asText());
                case "cancelled": throw new Cancelled();
                default: pause();
            }
        }
    }

    private void request(String id, String field, String value) {
        checkpoint(id);
        store.update(id, j -> j.put(field, value));
    }

    private boolean cleanup(String id) {
        boolean complete = true;
        var job = store.get(id);
        for (String field : new String[]{"referenceRequest", "viewsRequest", "trainRequest"}) {
            if (!job.hasNonNull(field)) continue;
            boolean train = field.equals("trainRequest");
            try {
                var found = tools.call(train ? "webscan_get_request" : "creative_get_request", Map.of("request_id", job.path(field).asText())).path("job");
                if (found.isMissingNode() || found.isNull()) continue;
                String child = found.path(train ? "job_id" : "id").asText();
                if (!child.isEmpty()) tools.call(train ? "webscan_cancel_reconstruction" : "creative_cancel_job", Map.of("job_id", child));
            } catch (RuntimeException e) { complete = false; }
        }
        return complete;
    }

    private void finishCancellation(String id) {
        if (GraphicsJobStore.terminal(store.get(id))) return;
        boolean complete = cleanup(id);
        store.update(id, j -> j.put("status", complete ? "CANCELLED" : "CANCELLING")
                .put("message", complete ? "任务已取消；已发送的模型请求可能仍由供应商完成。"
                        : "尚未确认下游任务停止。请恢复 MCP 服务后再次点击取消。"));
    }

    private void checkpoint(String id) {
        if (Thread.currentThread().isInterrupted() || store.get(id).path("cancelRequested").asBoolean()) throw new Cancelled();
    }
    private void checkDeadline(Instant deadline) {
        if (Instant.now().isAfter(deadline)) throw new IllegalStateException("任务超过最长等待时间，已请求停止下游任务");
    }
    private void pause() {
        try { Thread.sleep(properties.getPollInterval().toMillis()); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new Cancelled(); }
    }
    private void stage(String id, String status, String stage, String message) {
        checkpoint(id);
        store.update(id, j -> j.put("status", status).put("stage", stage).put("message", message));
    }
    private static String required(JsonNode node, String field) {
        String value = node.path(field).asText();
        if (!value.matches("[a-f0-9]{32}")) throw new IllegalStateException("工具返回的资源 ID 无效");
        return value;
    }
    private static String safeMessage(RuntimeException e) {
        String value = e.getMessage();
        return value == null ? "任务未完成，请检查本地服务" : value.substring(0, Math.min(800, value.length()));
    }
    @PreDestroy
    public void close() throws InterruptedException {
        worker.shutdownNow();
        worker.awaitTermination(5, TimeUnit.SECONDS);
    }
}
