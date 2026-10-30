package com.lucius.springaitest.graphics;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.function.Consumer;
import jakarta.annotation.PreDestroy;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("graphics")
public class GraphicsJobStore {
    public static final Set<String> TERMINAL = Set.of("READY", "CONCEPT_READY", "FAILED", "NEEDS_REVIEW", "CANCELLED");
    private final Path directory;
    private final ObjectMapper mapper;
    private final FileChannel lockChannel;
    private final FileLock lock;
    public record Creation(ObjectNode job, boolean created) {}

    public GraphicsJobStore(GraphicsProperties properties, ObjectMapper mapper) throws IOException {
        this.mapper = mapper;
        directory = properties.getDataDir().resolve("workflows");
        Files.createDirectories(directory);
        lockChannel = FileChannel.open(directory.resolve(".lock"), StandardOpenOption.CREATE, StandardOpenOption.WRITE);
        lock = lockChannel.tryLock();
        if (lock == null) throw new IllegalStateException("已有 Agent 使用这个数据目录");
    }

    public synchronized Creation create(String prompt, String mode, String parentId, String key) {
        for (ObjectNode job : list()) {
            if (job.path("requestKey").asText().equals(key)) {
                if (!job.path("prompt").asText().equals(prompt) || !job.path("mode").asText().equals(mode)
                        || !job.path("parentJobId").asText().equals(parentId == null ? "" : parentId))
                    throw new IllegalArgumentException("同一请求标识不能用于不同内容");
                return new Creation(job, false);
            }
        }
        if (parentId != null && !parentId.isBlank()) get(parentId);
        if (list().stream().filter(j -> !terminal(j)).count() >= 16) throw new IllegalArgumentException("任务队列已满，请稍后再试");
        String now = Instant.now().toString();
        ObjectNode job = mapper.createObjectNode().put("id", UUID.randomUUID().toString().replace("-", ""))
                .put("prompt", prompt).put("mode", mode).put("parentJobId", parentId == null ? "" : parentId)
                .put("requestKey", key).put("status", "QUEUED").put("stage", "queued")
                .put("message", "等待开始").put("cancelRequested", false).put("createdAt", now).put("updatedAt", now);
        job.putArray("referenceAssets");
        job.putArray("viewAssets");
        save(job);
        return new Creation(job.deepCopy(), true);
    }

    public synchronized ObjectNode get(String id) {
        if (id == null || !id.matches("[a-f0-9]{32}")) throw new IllegalArgumentException("无效任务 ID");
        Path path = directory.resolve(id + ".json");
        if (!Files.exists(path)) throw new java.util.NoSuchElementException("任务不存在");
        try { return (ObjectNode) mapper.readTree(path.toFile()); }
        catch (IOException e) { throw new IllegalStateException("无法读取本地任务记录", e); }
    }

    public synchronized ObjectNode getByRequestKey(String key) {
        if (key == null || !key.matches("[a-zA-Z0-9._:-]{1,160}"))
            throw new IllegalArgumentException("请求标识格式错误");
        return list().stream().filter(job -> job.path("requestKey").asText().equals(key)).findFirst()
                .orElseThrow(() -> new java.util.NoSuchElementException("尚未找到这个提交对应的任务"));
    }

    public synchronized List<ObjectNode> list() {
        try (var files = Files.list(directory)) {
            return files.filter(p -> p.getFileName().toString().matches("[a-f0-9]{32}\\.json"))
                    .map(p -> get(p.getFileName().toString().substring(0, 32)))
                    .sorted(Comparator.comparing((ObjectNode j) -> j.path("createdAt").asText()).reversed()).toList();
        } catch (IOException e) { throw new IllegalStateException("无法读取本地任务目录", e); }
    }

    public synchronized ObjectNode update(String id, Consumer<ObjectNode> changes) {
        ObjectNode job = get(id);
        if (terminal(job)) return job;
        changes.accept(job);
        // Cancellation wins over late success and intermediate progress updates.
        if (job.path("cancelRequested").asBoolean() && !"CANCELLED".equals(job.path("status").asText()))
            job.put("status", "CANCELLING");
        job.put("updatedAt", Instant.now().toString());
        save(job);
        return job.deepCopy();
    }

    private void save(ObjectNode job) {
        Path path = directory.resolve(job.path("id").asText() + ".json");
        Path temp = path.resolveSibling(path.getFileName() + ".tmp");
        try {
            mapper.writerWithDefaultPrettyPrinter().writeValue(temp.toFile(), job);
            Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) { throw new IllegalStateException("无法保存本地任务记录", e); }
    }

    public static boolean terminal(ObjectNode job) { return TERMINAL.contains(job.path("status").asText()); }

    @PreDestroy
    public void close() throws IOException {
        lock.release();
        lockChannel.close();
    }
}
