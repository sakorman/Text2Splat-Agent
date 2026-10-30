package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.modelcontextprotocol.client.McpClient;
import io.modelcontextprotocol.client.McpSyncClient;
import io.modelcontextprotocol.client.transport.WebClientStreamableHttpTransport;
import io.modelcontextprotocol.spec.McpSchema;
import jakarta.annotation.PreDestroy;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

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
        JsonNode body;
        try {
            var result = client(server).callTool(new McpSchema.CallToolRequest(name, arguments));
            if (Boolean.TRUE.equals(result.isError())) throw new IllegalStateException("MCP tool error");
            if (result.structuredContent() != null) body = mapper.valueToTree(result.structuredContent());
            else {
                var content = result.content().stream().filter(McpSchema.TextContent.class::isInstance)
                        .map(McpSchema.TextContent.class::cast).findFirst().orElseThrow();
                body = mapper.readTree(content.text());
            }
        } catch (Exception e) {
            disconnect(server);
            // No mutation retry: a timeout may have happened after the server accepted the job.
            throw new IllegalStateException(server + " MCP 暂不可用或请求结果未知，请检查本地服务", e);
        }
        if (!body.path("ok").asBoolean()) throw new IllegalStateException(body.path("error").asText("MCP 操作失败"));
        if (!body.has("data")) throw new IllegalStateException("MCP 返回内容不完整");
        return body.get("data");
    }

    private synchronized void disconnect(String name) {
        var client = clients.remove(name);
        if (client != null) client.close();
    }

    @PreDestroy
    public synchronized void close() {
        clients.values().forEach(McpSyncClient::close);
        clients.clear();
    }
}
