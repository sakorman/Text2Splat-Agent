package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.*;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("graphics")
class GraphicsApiTest {
    static final Path DATA;
    static { try { DATA = Files.createTempDirectory("graphics-api-test-"); } catch (Exception e) { throw new RuntimeException(e); } }
    @DynamicPropertySource static void properties(DynamicPropertyRegistry registry) {
        registry.add("graphics.data-dir", () -> DATA.toString());
    }
    @Autowired TestRestTemplate http;
    @Autowired ObjectMapper mapper;
    @MockitoBean GraphicsTools tools;

    @Test void standaloneProfileStartsWithoutDatabasesAndServesUi() {
        var page = http.getForEntity("/graphics/", String.class);
        assertThat(page.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(page.getBody()).contains("Text2Splat Agent").contains("造物方舟").contains("studio.js");
        assertThat(page.getHeaders().getFirst("X-Content-Type-Options")).isEqualTo("nosniff");
        assertThat(page.getHeaders().getCacheControl()).isEqualTo("no-cache");
    }
    @Test void invalidInputAndCrossOriginMutationAreRejected() {
        var bad = http.postForEntity("/api/graphics/jobs", Map.of("prompt", "", "mode", "THREE_D"), Map.class);
        assertThat(bad.getStatusCode()).isEqualTo(HttpStatus.BAD_REQUEST);
        var headers = new HttpHeaders(); headers.setContentType(MediaType.APPLICATION_JSON); headers.setOrigin("https://untrusted.example");
        var cross = http.exchange("/api/graphics/jobs", HttpMethod.POST, new HttpEntity<>(Map.of("prompt", "一个花瓶"), headers), String.class);
        assertThat(cross.getStatusCode()).isEqualTo(HttpStatus.FORBIDDEN);
    }
    @Test void jobIsAcceptedAsynchronouslyAndMissingKeyIsVisible() throws Exception {
        when(tools.call(eq("creative_health"), anyMap())).thenReturn(mapper.createObjectNode().put("configured", false));
        var headers = new HttpHeaders(); headers.setContentType(MediaType.APPLICATION_JSON); headers.set("Idempotency-Key", "api-test");
        var request = new HttpEntity<>(Map.of("prompt", "一个青瓷花瓶", "mode", "CONCEPT"), headers);
        var accepted = http.postForEntity("/api/graphics/jobs", request, Map.class);
        assertThat(accepted.getStatusCode()).isEqualTo(HttpStatus.ACCEPTED);
        String id = accepted.getBody().get("id").toString();
        var duplicate = http.postForEntity("/api/graphics/jobs", request, Map.class);
        assertThat(duplicate.getBody().get("id")).isEqualTo(id);
        Map job = Map.of();
        for (int i=0; i<100; i++) {
            job = http.getForObject("/api/graphics/jobs/" + id, Map.class);
            if ("FAILED".equals(job.get("status"))) break;
            Thread.sleep(20);
        }
        assertThat(job.get("status")).isEqualTo("FAILED");
        assertThat(job.get("message").toString()).contains("ZHIPU_API_KEY");
        var events = http.getForEntity("/api/graphics/jobs/" + id + "/events", String.class);
        assertThat(events.getBody()).contains("event:progress").contains("FAILED");
    }
    @Test void nonexistentAssetsAreNotServed() {
        assertThat(http.getForEntity("/api/graphics/assets/" + "a".repeat(32) + "/00.png", String.class).getStatusCode()).isEqualTo(HttpStatus.NOT_FOUND);
    }
}
