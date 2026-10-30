package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.node.ObjectNode;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.nio.file.Files;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.UUID;
import org.springframework.context.annotation.Profile;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;

@RestController
@Profile("graphics")
@RequestMapping("/api/graphics")
public class GraphicsController {
    private final GraphicsWorkflow workflow;
    private final GraphicsJobStore store;
    private final GraphicsProperties properties;
    public record CreateRequest(@NotBlank @Size(min=2, max=2000) String prompt,
                                @Pattern(regexp="CONCEPT|THREE_D") String mode,
                                @Pattern(regexp="[a-f0-9]{32}") String parentJobId) {}

    public GraphicsController(GraphicsWorkflow workflow, GraphicsJobStore store, GraphicsProperties properties) {
        this.workflow = workflow; this.store = store; this.properties = properties;
    }
    @GetMapping("/health")
    public ObjectNode health() { return workflow.health(); }

    @PostMapping(value="/jobs", consumes=MediaType.APPLICATION_JSON_VALUE)
    public ResponseEntity<ObjectNode> create(@Valid @RequestBody CreateRequest body,
                                             @RequestHeader(value="Idempotency-Key", required=false) String key) {
        if (key == null) key = UUID.randomUUID().toString();
        if (!key.matches("[a-zA-Z0-9._:-]{1,160}")) throw new IllegalArgumentException("请求标识格式错误");
        var job = workflow.submit(body.prompt().strip(), body.mode() == null ? "CONCEPT" : body.mode(), body.parentJobId(), key);
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(job);
    }
    @GetMapping("/jobs")
    public List<ObjectNode> list() { return store.list().stream().limit(50).toList(); }

    @GetMapping("/jobs/{id}")
    public ObjectNode get(@PathVariable String id) { return store.get(id); }

    @PostMapping("/jobs/{id}/cancel")
    public ObjectNode cancel(@PathVariable String id) { return workflow.cancel(id); }

    @GetMapping(value="/jobs/{id}/events", produces=MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<ObjectNode>> events(@PathVariable String id) {
        store.get(id);
        return Flux.interval(Duration.ZERO, Duration.ofSeconds(1))
                .map(tick -> store.get(id)).takeUntil(GraphicsJobStore::terminal)
                .map(job -> ServerSentEvent.<ObjectNode>builder(job).event("progress").id(job.path("updatedAt").asText()).build());
    }
    @GetMapping("/assets/{setId}/{name}")
    public ResponseEntity<Resource> asset(@PathVariable String setId, @PathVariable String name) {
        if (!setId.matches("[a-f0-9]{32}") || !name.matches("[0-9]{2}\\.png")) throw new NoSuchElementException("图片不存在");
        var directory = properties.getDataDir().resolve("assets").resolve(setId);
        var path = directory.resolve(name);
        if (Files.isSymbolicLink(directory) || !Files.isRegularFile(path, java.nio.file.LinkOption.NOFOLLOW_LINKS))
            throw new NoSuchElementException("图片不存在");
        return ResponseEntity.ok().contentType(MediaType.IMAGE_PNG).body(new FileSystemResource(path));
    }

    @RestControllerAdvice(assignableTypes=GraphicsController.class)
    static class Errors {
        @ExceptionHandler(NoSuchElementException.class)
        ResponseEntity<Map<String,String>> missing(NoSuchElementException e) {
            return ResponseEntity.status(404).body(Map.of("message", e.getMessage()));
        }
        @ExceptionHandler({IllegalArgumentException.class, MethodArgumentNotValidException.class})
        ResponseEntity<Map<String,String>> invalid(Exception e) {
            return ResponseEntity.badRequest().body(Map.of("message", e instanceof MethodArgumentNotValidException
                    ? "请提供 2–2000 字描述和有效的生成模式" : e.getMessage()));
        }
        @ExceptionHandler(IllegalStateException.class)
        ResponseEntity<Map<String,String>> unavailable(IllegalStateException e) {
            return ResponseEntity.status(503).body(Map.of("message", "本地任务服务暂不可用，请检查服务日志和磁盘空间"));
        }
    }
}
