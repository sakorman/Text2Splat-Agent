package com.lucius.springaitest.graphics;

import java.net.URI;
import java.nio.file.Path;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("graphics")
@ConfigurationProperties("graphics")
public class GraphicsProperties {
    private Path dataDir = Path.of(".graphics");
    private String creativeMcpUrl = "http://127.0.0.1:8766";
    private String webscanMcpUrl = "http://127.0.0.1:8767";
    private Duration pollInterval = Duration.ofSeconds(2);
    private Duration jobTimeout = Duration.ofHours(2);
    public Path getDataDir() { return dataDir.toAbsolutePath().normalize(); }
    public void setDataDir(Path value) { dataDir = value; }
    public String getCreativeMcpUrl() { return creativeMcpUrl; }
    public void setCreativeMcpUrl(String value) { creativeMcpUrl = localUrl(value); }
    public String getWebscanMcpUrl() { return webscanMcpUrl; }
    public void setWebscanMcpUrl(String value) { webscanMcpUrl = localUrl(value); }
    public Duration getPollInterval() { return pollInterval; }
    public void setPollInterval(Duration value) {
        if (value.isNegative() || value.isZero()) throw new IllegalArgumentException("轮询间隔必须大于零");
        pollInterval = value;
    }
    public Duration getJobTimeout() { return jobTimeout; }
    public void setJobTimeout(Duration value) { jobTimeout = value; }
    static String localUrl(String value) {
        URI uri = URI.create(value);
        if (!"http".equals(uri.getScheme()) || !java.util.Set.of("127.0.0.1", "localhost", "[::1]").contains(uri.getHost())
                || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null) {
            throw new IllegalArgumentException("MCP 必须使用本机 HTTP 地址");
        }
        return value.replaceAll("/+$", "");
    }
}
