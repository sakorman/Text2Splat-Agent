package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.modelcontextprotocol.client.McpClient;
import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.client.transport.WebClientStreamableHttpTransport;
import io.modelcontextprotocol.spec.McpSchema;
import io.modelcontextprotocol.spec.McpError;
import io.modelcontextprotocol.spec.McpTransportException;
import io.modelcontextprotocol.spec.McpTransportSessionClosedException;
import io.modelcontextprotocol.spec.McpTransportSessionNotFoundException;
import com.fasterxml.jackson.core.JsonProcessingException;
import jakarta.annotation.PreDestroy;
import java.io.IOException;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeoutException;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientRequestException;
import org.springframework.web.reactive.function.client.WebClientResponseException;

@Component
@Profile("graphics")
public class McpGraphicsTools implements GraphicsTools {
    private static final Set<String> ALLOWED = Set.of("creative_health", "creative_generate_reference",
            "creative_generate_multiview", "creative_get_job", "creative_cancel_job", "webscan_health",
            "webscan_import_generated_views", "webscan_start_reconstruction", "webscan_get_status",
            "webscan_get_result", "webscan_cancel_reconstruction", "creative_get_request", "webscan_get_request");
    private final GraphicsProperties properties;
    private final ObjectMapper mapper;
    private final Map<String, McpSyncClient> clients = new HashMap<>();

    public McpGraphicsTools(GraphicsProperties properties, ObjectMapper mapper) {
        this.properties = properties;
        this.mapper = mapper;
    }

    private synchronized McpSyncClient client(String server) {
        if (clients.containsKey(server)) return clients.get(server);
        String url = server.equals("creative") ? properties.getCreativeMcpUrl() : properties.getWebscanMcpUrl();
        var transport = WebClientStreamableHttpTransport.builder(WebClient.builder().baseUrl(url)
                        .codecs(c -> c.defaultCodecs().maxInMemorySize(2 * 1024 * 1024)))
                .endpoint("/mcp").openConnectionOnStartup(false).build();
        var client = McpClient.sync(transport).requestTimeout(Duration.ofSeconds(90))
                .initializationTimeout(Duration.ofSeconds(5))
                .clientInfo(new McpSchema.Implementation("text2splat-agent", "0.1.0")).build();
        try {
            client.initialize();
            clients.put(server, client);
            return client;
        } catch (RuntimeException e) {
            client.close();
            throw e;
        }
    }

    @Override
    public JsonNode call(String name, Map<String, Object> arguments) {
        if (!ALLOWED.contains(name)) throw new IllegalArgumentException("未知图形工具");
        String server = name.startsWith("creative_") ? "creative" : "webscan";
        McpSchema.CallToolResult result;
        try {
            result = client(server).callTool(new McpSchema.CallToolRequest(name, arguments));
        } catch (RuntimeException e) {
            disconnect(server);
            // No retry here: a timed-out mutation may already have been accepted.
            String message = server + " MCP 暂不可用或请求结果未知，请检查本地服务";
            if (isTemporaryConnectionFailure(e)) throw new GraphicsToolUnavailableException(message, e);
            throw new IllegalStateException(message, e);
        }
        return decodeResult(result);
    }

    JsonNode decodeResult(McpSchema.CallToolResult result) {
        if (result == null || Boolean.TRUE.equals(result.isError()))
            throw new IllegalStateException("MCP 工具执行失败，请检查输入和配置");
        JsonNode body;
        try {
            if (result.structuredContent() != null) body = mapper.valueToTree(result.structuredContent());
            else {
                var content = result.content().stream().filter(McpSchema.TextContent.class::isInstance)
                        .map(McpSchema.TextContent.class::cast).findFirst().orElseThrow();
                body = mapper.readTree(content.text());
            }
        } catch (Exception e) {
            throw new IllegalStateException("MCP 返回内容无法解析，请检查本地服务版本", e);
        }
        if (body == null || !body.path("ok").isBoolean()) throw new IllegalStateException("MCP 返回内容不完整");
        if (!body.path("ok").asBoolean()) {
            String message = body.path("error").asText("MCP 操作失败");
            if ("TEMPORARY_UNAVAILABLE".equals(body.path("error_code").asText()))
                throw new GraphicsToolUnavailableException(message);
            throw new IllegalStateException(message);
        }
        if (!body.hasNonNull("data")) throw new IllegalStateException("MCP 返回内容不完整");
        return body.get("data");
    }

    static boolean isTemporaryConnectionFailure(Throwable failure) {
        boolean temporary = false;
        var seen = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<Throwable, Boolean>());
        for (Throwable cause = failure; cause != null && seen.add(cause); cause = cause.getCause()) {
            if (cause instanceof InterruptedException || cause instanceof java.util.concurrent.CancellationException
                    || cause instanceof JsonProcessingException) return false;
            if (cause instanceof McpError error && error.getJsonRpcError() != null) return false;
            if (cause instanceof WebClientResponseException response)
                return Set.of(408, 429, 500, 502, 503, 504).contains(response.getStatusCode().value());
            if (cause instanceof IOException || cause instanceof TimeoutException
                    || cause instanceof WebClientRequestException || cause instanceof McpTransportException
                    || cause instanceof McpTransportSessionClosedException || cause instanceof McpTransportSessionNotFoundException)
                temporary = true;
        }
        return temporary;
    }

    private synchronized void disconnect(String name) {
        var client = clients.remove(name);
        if (client != null) {
            try { client.close(); }
            catch (RuntimeException ignored) { /* Preserve the original connection failure. */ }
        }
    }

    @PreDestroy
    public synchronized void close() {
        clients.values().forEach(McpSyncClient::close);
        clients.clear();
    }
}
