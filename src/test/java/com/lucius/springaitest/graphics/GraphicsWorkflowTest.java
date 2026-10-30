package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import static org.assertj.core.api.Assertions.*;

class GraphicsWorkflowTest {
    @TempDir Path root;
    ObjectMapper mapper = new ObjectMapper();
    GraphicsProperties properties;
    GraphicsJobStore store;
    FakeTools tools;
    GraphicsWorkflow workflow;

    @BeforeEach void setup() throws Exception {
        properties = new GraphicsProperties(); properties.setDataDir(root);
        properties.setPollInterval(Duration.ofMillis(5)); properties.setJobTimeout(Duration.ofSeconds(10));
        store = new GraphicsJobStore(properties, mapper);
        tools = new FakeTools(); workflow = new GraphicsWorkflow(store, tools, properties, mapper);
    }
    @AfterEach void close() throws Exception { tools.release.countDown(); workflow.close(); store.close(); }

    @Test void fullPipelineProducesRealResultLinkOnlyAfterReconstruction() throws Exception {
        String id = workflow.submit("青瓷花瓶", "THREE_D", null, "first").path("id").asText();
        var result = awaitTerminal(id);
        assertThat(result.path("status").asText()).isEqualTo("READY");
        assertThat(result.path("result").path("viewer_url").asText()).contains("127.0.0.1");
        assertThat(tools.starts.get()).isEqualTo(1);
        assertThat(result.path("referenceAssets").size()).isEqualTo(1);
    }

    @Test void conceptModeDoesNotStartMultiviewOrTraining() throws Exception {
        String id = workflow.submit("熊猫木雕", "CONCEPT", null, "concept").path("id").asText();
        assertThat(awaitTerminal(id).path("status").asText()).isEqualTo("CONCEPT_READY");
        assertThat(tools.multiview.get()).isZero(); assertThat(tools.starts.get()).isZero();
    }

    @Test void idempotentRequestsNeverLaunchAgainAndChangedContentIsRejected() throws Exception {
        var first = workflow.submit("青瓷花瓶", "CONCEPT", null, "same");
        String id = first.path("id").asText();
        assertThat(workflow.submit("青瓷花瓶", "CONCEPT", null, "same").path("id").asText()).isEqualTo(id);
        awaitTerminal(id);
        assertThat(tools.references.get()).isEqualTo(1);
        assertThatThrownBy(() -> workflow.submit("另一只花瓶", "CONCEPT", null, "same")).isInstanceOf(IllegalArgumentException.class);
    }

    @Test void appearanceFailureKeepsViewsAndNeverStartsTraining() throws Exception {
        tools.rejectViews = true;
        String id = workflow.submit("一个复杂物件", "THREE_D", null, "review").path("id").asText();
        var job = awaitTerminal(id);
        assertThat(job.path("status").asText()).isEqualTo("NEEDS_REVIEW");
        assertThat(job.path("viewAssets").size()).isEqualTo(1);
        assertThat(tools.starts.get()).isZero();
        assertThat(job.has("result")).isFalse();
    }

    @Test void reconstructionFailureNeverClaimsReady() throws Exception {
        tools.failTraining = true;
        String id = workflow.submit("一个花瓶", "THREE_D", null, "sfm-fail").path("id").asText();
        var job = awaitTerminal(id);
        assertThat(job.path("status").asText()).isEqualTo("NEEDS_REVIEW");
        assertThat(job.path("message").asText()).contains("匹配不足");
        assertThat(job.has("result")).isFalse();
    }

    @Test void cancellationWinsOverLateProviderSuccess() throws Exception {
        tools.block = true;
        String id = workflow.submit("一个花瓶", "THREE_D", null, "cancel").path("id").asText();
        assertThat(tools.entered.await(3, TimeUnit.SECONDS)).isTrue();
        workflow.cancel(id);
        tools.release.countDown();
        assertThat(awaitTerminal(id).path("status").asText()).isEqualTo("CANCELLED");
        assertThat(tools.starts.get()).isZero();
        assertThat(tools.cancels.get()).isPositive();
    }

    @Test void lostSubmitResponseIsLookedUpAndCancelledWithoutResubmission() throws Exception {
        tools.loseResponse = true;
        String id = workflow.submit("一个花瓶", "CONCEPT", null, "lost").path("id").asText();
        assertThat(awaitTerminal(id).path("status").asText()).isEqualTo("FAILED");
        assertThat(tools.references.get()).isEqualTo(1);
        assertThat(tools.cancels.get()).isEqualTo(1);
    }

    @Test void restartMarksInterruptedWorkflowAndDoesNotRegenerate() throws Exception {
        var job = store.create("一个花瓶", "CONCEPT", null, "restart").job();
        String id = job.path("id").asText();
        store.update(id, j -> j.put("status", "REFERENCE").put("referenceRequest", id + ":reference"));
        workflow.recover();
        assertThat(awaitTerminal(id).path("status").asText()).isEqualTo("FAILED");
        assertThat(tools.references.get()).isZero();
        assertThat(tools.cancels.get()).isEqualTo(1);
    }

    @Test void persistedStateAndInvalidIdsAreHandled() throws Exception {
        var job = store.create("一个花瓶", "CONCEPT", null, "persist").job();
        String id = job.path("id").asText();
        store.update(id, j -> j.put("status", "CANCELLED"));
        store.update(id, j -> j.put("status", "READY"));
        assertThat(store.get(id).path("status").asText()).isEqualTo("CANCELLED");
        assertThatThrownBy(() -> store.get("../../etc/passwd")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> properties.setCreativeMcpUrl("https://remote.example.org")).isInstanceOf(IllegalArgumentException.class);
    }

    private ObjectNode awaitTerminal(String id) throws Exception {
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < end) {
            var job = store.get(id);
            if (GraphicsJobStore.terminal(job)) return job;
            Thread.sleep(10);
        }
        throw new AssertionError(store.get(id).toPrettyString());
    }

    class FakeTools implements GraphicsTools {
        AtomicInteger references = new AtomicInteger(), multiview = new AtomicInteger(), starts = new AtomicInteger(), cancels = new AtomicInteger();
        boolean rejectViews, failTraining, block, loseResponse;
        CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
        final String ref = "1".repeat(32), views = "2".repeat(32), train = "3".repeat(32), session = "4".repeat(32);
        @Override public JsonNode call(String name, Map<String,Object> args) {
            var node = mapper.createObjectNode();
            switch (name) {
                case "creative_health": return node.put("configured", true);
                case "webscan_health": return node.put("available", true).put("brush_available", true);
                case "creative_generate_reference":
                    references.incrementAndGet();
                    if (loseResponse) throw new IllegalStateException("请求结果未知");
                    return node.put("id", ref);
                case "creative_generate_multiview": multiview.incrementAndGet(); return node.put("id", views);
                case "creative_get_job":
                    if (block) {
                        entered.countDown();
                        try { release.await(3, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
                    }
                    boolean multi = views.equals(args.get("job_id"));
                    node.put("status", multi && rejectViews ? "needs_review" : "succeeded").put("stage", multi ? "review" : "reference")
                            .put("asset_set_id", multi ? views : ref).put("message", "外观检查结果");
                    node.putObject("spec").put("title", "测试花瓶");
                    node.putArray("assets").addObject().put("url", "/api/graphics/assets/" + ref + "/00.png");
                    return node;
                case "webscan_import_generated_views": return node.put("session_id", session);
                case "webscan_start_reconstruction": starts.incrementAndGet(); return node.put("job_id", train);
                case "webscan_get_status": return node.put("status", failTraining ? "failed" : "succeeded").put("stage", "sfm").put("message", "匹配不足");
                case "webscan_get_result": return node.put("viewer_url", "http://127.0.0.1:8787/?p=" + session + "&j=" + train);
                case "creative_get_request": node.putObject("job").put("id", ref); return node;
                case "webscan_get_request": node.putObject("job").put("job_id", train); return node;
                case "creative_cancel_job", "webscan_cancel_reconstruction": cancels.incrementAndGet(); return node.put("status", "cancelled");
                default: throw new AssertionError(name);
            }
        }
    }
}
