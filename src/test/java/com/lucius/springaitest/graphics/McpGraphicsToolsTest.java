package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.modelcontextprotocol.spec.McpError;
import io.modelcontextprotocol.spec.McpSchema;
import java.io.IOException;
import java.util.Map;
import java.util.concurrent.TimeoutException;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import static org.assertj.core.api.Assertions.*;

class McpGraphicsToolsTest {
    ObjectMapper mapper = new ObjectMapper();
    McpGraphicsTools tools = new McpGraphicsTools(new GraphicsProperties(), mapper);

    @Test @Timeout(15) void realSdkConnectionRefusalIsClassifiedWithoutExposingTransportDetails() throws Exception {
        int port;
        try (var socket = new java.net.ServerSocket(0)) { port = socket.getLocalPort(); }
        var properties = new GraphicsProperties();
        properties.setCreativeMcpUrl("http://127.0.0.1:" + port);
        var offline = new McpGraphicsTools(properties, mapper);
        try {
            assertThatThrownBy(() -> offline.call("creative_get_job", Map.of("job_id", "a".repeat(32))))
                    .isInstanceOf(GraphicsToolUnavailableException.class)
                    .hasMessage("creative MCP 暂不可用或请求结果未知，请检查本地服务");
        } finally { offline.close(); }
    }

    @Test void connectionErrorsAreDistinctFromProtocolAndParsingFailures() throws Exception {
        assertThat(McpGraphicsTools.isTemporaryConnectionFailure(new RuntimeException(new TimeoutException()))).isTrue();
        assertThat(McpGraphicsTools.isTemporaryConnectionFailure(new IOException())).isTrue();
        assertThat(McpGraphicsTools.isTemporaryConnectionFailure(new IllegalArgumentException())).isFalse();
        assertThat(McpGraphicsTools.isTemporaryConnectionFailure(new RuntimeException(new InterruptedException()))).isFalse();
        var protocol = new McpError(new McpSchema.JSONRPCResponse.JSONRPCError(-32602, "invalid parameters", null));
        assertThat(McpGraphicsTools.isTemporaryConnectionFailure(protocol)).isFalse();
        try { mapper.readTree("not-json"); }
        catch (IOException e) { assertThat(McpGraphicsTools.isTemporaryConnectionFailure(e)).isFalse(); }
    }

    @Test void onlyTemporaryHttpStatusesAreRetryable() {
        for (int status : new int[]{408, 429, 500, 502, 503, 504})
            assertThat(McpGraphicsTools.isTemporaryConnectionFailure(
                    WebClientResponseException.create(status, "test", null, null, null))).isTrue();
        for (int status : new int[]{400, 401, 403, 404, 409, 422})
            assertThat(McpGraphicsTools.isTemporaryConnectionFailure(
                    WebClientResponseException.create(status, "test", null, null, null))).isFalse();
    }

    @Test void adapterErrorCodeIsRequiredForRetryableToolResults() {
        var transientResult = McpSchema.CallToolResult.builder().structuredContent(Map.of(
                "ok", false, "error", "暂不可用", "error_code", "TEMPORARY_UNAVAILABLE")).build();
        assertThatThrownBy(() -> tools.decodeResult(transientResult)).isInstanceOf(GraphicsToolUnavailableException.class);
        var invalid = McpSchema.CallToolResult.builder().structuredContent(Map.of("ok", false, "error", "图片不一致")).build();
        assertThatThrownBy(() -> tools.decodeResult(invalid)).isExactlyInstanceOf(IllegalStateException.class);
        var protocolError = McpSchema.CallToolResult.builder().isError(true).addTextContent("private failure details").build();
        assertThatThrownBy(() -> tools.decodeResult(protocolError)).isExactlyInstanceOf(IllegalStateException.class)
                .hasMessageNotContaining("private");
    }

    @Test void malformedToolDataIsNeverTreatedAsTemporary() {
        var malformed = McpSchema.CallToolResult.builder().addTextContent("not-json").build();
        assertThatThrownBy(() -> tools.decodeResult(malformed)).isExactlyInstanceOf(IllegalStateException.class);
        var missing = McpSchema.CallToolResult.builder().structuredContent(Map.of("ok", true)).build();
        assertThatThrownBy(() -> tools.decodeResult(missing)).isExactlyInstanceOf(IllegalStateException.class);
        var valid = McpSchema.CallToolResult.builder().structuredContent(Map.of("ok", true, "data", Map.of("status", "running"))).build();
        assertThat(tools.decodeResult(valid).path("status").asText()).isEqualTo("running");
    }
}
