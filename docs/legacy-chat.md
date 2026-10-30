> 历史文档：以下为旧聊天模块说明。当前版本、启动方式和环境变量以根目录 README 为准。

# SpringAI-Agent

**基于 Spring Boot 的 AI 对话代理框架，支持多模型集成与实时流式交互**

---

## 🚀 项目简介

SpringAI-Agent 是一个基于 Java 的 AI 对话代理框架，构建于 **Spring Boot 3.4.4** 之上，通过 **Spring AI 1.0.0-M7**
集成大型语言模型（LLM），提供灵活的 REST API 接口，支持多种场景下的 AI 交互体验。- [前端代码仓库](https://github.com/LuciusWan/Spring-AI-Agent-FrontEnd)

项目核心功能包括：

- 多 AI 模型支持（OpenAI/Aliyun/Ollama）
- 实时流式响应（SSE/Flux）
- 自定义聊天客户端（如 SQL 咨询、编程助手、模拟伴侣等）
- Redis + MySQL 混合存储
- 工具扩展（CourseTools、CodeTools）

---

## 🧱 技术栈

| 技术                     | 用途                  |
|------------------------|---------------------|
| **Spring Boot 3.4.4**  | 核心应用框架              |
| **Spring AI 1.0.0-M7** | AI 模型集成与对话管理        |
| **Redis**              | 聊天上下文缓存（ChatMemory） |
| **MySQL**              | 课程信息等持久化数据存储        |
| **Java 17**            | 编程语言                |
| **Maven**              | 依赖管理                |
| **Spring Webflux**     | 实现 SSE 流式响应         |

## API接口

| 端点                        | 方法   | 说明          |
|---------------------------|------|-------------|
| `/ai/chat`                | POST | SSE流式对话接口   |
| `/ai/chat1`               | POST | 支持文件上传的对话接口 |
| `/ai/history/{type}`      | GET  | 按类型查询对话ID列表 |
| `/ai/history/{type}/{id}` | GET  | 获取指定对话的历史消息 |

---

## 🧩 系统架构

SpringAI-Agent 采用分层架构设计：

```
Client Applications
  ↓
**Controller Layer** (AIController, CourseController)
  ↓
**Service Layer** (ChatService, ChatMemory)
  ↓
**Chat Clients** (DefaultChatClient, SQLChatClient, ...)
  ↓
**AI Models** (OpenAI/Aliyun/Ollama)
  ↓
**Storage** (Redis, MySQL)
```

### 核心流程（流式交互示例）

1. **请求处理**：客户端通过 `/ai/chat` 发送对话请求。
2. **上下文加载**：从 Redis 中读取历史记录（`REDIS_MEMORY_ID + chatId`）。
3. **模型调用**：将上下文拼接后发送至 AI 模型（OpenAI/Ollama）。
4. **流式响应**：通过 **SSE** 或 **Flux<String>** 实时返回结果。
5. **存储更新**：将对话记录保存回 Redis。

---

## 🔧 关键组件

### 1. **Controller 层**

- **AIController**
    - 处理 AI 聊天请求（`/ai/chat`、`/ai/chat1`）
    - 支持文件上传（`/ai/chat1`）
    - 提供 SSE 流式响应（`SseEmitter`）
    - 管理聊天历史（`/ai/history/{type}`）

- **CourseController**
    - 处理课程信息与预约（如 `/course/list`）。

### 2. **Service 层**

- **ChatService**
    - 管理聊天会话 ID 与上下文。
- **ChatMemory**
    - 基于 Redis 的对话历史存储与检索。
- **扩展工具**
    - **CourseTools**：课程信息查询（整合 Redis + MySQL）。
    - **CodeTools**：代码生成与文件操作（模拟文件系统）。

### 3. **AI 模型集成**

支持以下模型：

- **OpenAI**（通过 Aliyun API）
- **Ollama**（本地部署）
- **Qwen-Omni-Turbo**（文件上传与图像分析专用）

## 4. **专用聊天客户端**

| 客户端名称                       | 用途       | 配置说明                    |
|-----------------------------|----------|-------------------------|
| `DefaultChatClient`         | 通用对话     | 默认提示词                   |
| `ComputerNetWorkChatClient` | 计算机网络问题  | 网络协议相关提示词               |
| `SQLChatClient`             | SQL 查询建议 | SQL 语法优化提示词             |
| `OminiChatClient`           | 文件/图像分析  | 使用 `qwen-omni-turbo` 模型 |
| `GirlfriendChatClient`      | 模拟伴侣对话   | 情感化提示词                  |
| `AICoding/AICodingNext`     | 编程助手     | 集成 `CodeTools` 生成代码     |

---

## 🛠️ 配置与扩展

### 1. **配置文件**

- `application.yml`
    - AI 模型 API 密钥（OpenAI/Aliyun）
    - Redis 和 MySQL 连接信息
    - 模型默认配置（如 `ollama.base.url`）

- `AIConfiguration.java`
    - 注册所有聊天客户端（`DefaultChatClient`, `SQLChatClient` 等）
    - 配置专用提示词（`AIConstant` 类）

- `ChatMemoryConfig.java`
    - 初始化 Redis 连接与缓存策略

## AI-Tools

### 代码工具(CodeTools)

```java

@Component
public class CodeTools {
    @Tool(description = "创建用户要求创建的文件,并且把生成的代码写入文件")
    public static boolean writeFile(@ToolParam(description = "用户要求创建的文件的名称") String fileName, @ToolParam(description = "你生成的代码的内容") String content) {
        String directoryPath = "C:\\Users\\86180\\Desktop\\新建文件夹 (2)";
        // 创建目录对象
        File directory = new File(directoryPath);

        // 如果目录不存在，则尝试创建它
        if (!directory.exists()) {
            if (!directory.mkdirs()) {
                System.out.println("无法创建目录: " + directoryPath);
                return false;
            }
        }

        // 创建文件对象
        File file = new File(directory, fileName);

        try (FileWriter fileWriter = new FileWriter(file);
             BufferedWriter writer = new BufferedWriter(fileWriter)) {

            // 写入内容
            writer.write(content);
            System.out.println("文件写入成功: " + file.getAbsolutePath());
            return true;

        } catch (IOException e) {
            System.err.println("写入文件时发生错误: ");
            return false;
        }
    }
}
```

- 自动生成代码框架
- 代码生成工具包

### 课程工具(CourseTools)

```java

@Component
public class CourseTools {
    @Autowired
    private StringRedisTemplate stringRedisTemplate;
    @Autowired
    private CourseMapper courseMapper;

    @Tool(description = "根据条件查询课程")
    public List<Course> selectCourse(@ToolParam(description = "查询的条件") CourseQuery query) {
        List<String> range = stringRedisTemplate.opsForList().range(RedisConstant.COURSE, 0, -1);
        if (range == null || range.isEmpty()) {
            return List.of();
        }
        List<Course> courses = new ArrayList<>();
        for (String s : range) {
            Course course = new Course();
            course.setName(s);
            course.setDuration(100L);
            course.setPrice(1000L);
            course.setType(query.getType());
            courses.add(course);
        }
        return courses;
    }

    @Tool(description = "查询校区列表")
    public List<School> querySchool() {
        List<String> range = stringRedisTemplate.opsForList().range(RedisConstant.SCHOOL, 0, -1);
        if (range == null || range.isEmpty()) {
            return List.of();
        }
        List<School> schools = new ArrayList<>();
        for (String s : range) {
            School school = new School();
            school.setName(s);
            schools.add(school);
        }
        return schools;
    }
}
```

- 课程信息管理

---

## 📦 使用示例

### 1. **启动服务**

```bash
# 安装依赖
mvn clean install

# 启动应用
java -jar target/springai-agent-1.0.jar
```

### 2. **发送流式聊天请求**

```http
POST /ai/chat HTTP/1.1
Content-Type: application/json

{
  "prompt": "How to optimize SQL queries?",
  "chatId": "sql-123",
  "clientId": "SQLChatClient"
}
```

**响应（SSE 流式）**：

```event-stream
data: {"content":"Use indexes on frequently queried columns."}
data: {"content":"Avoid SELECT *; specify required columns."}
```

---

## 🧪 扩展性设计

- **新增聊天客户端**
    1. 创建新类继承 `AbstractChatClient`
    2. 在 `AIConfiguration.java` 中注册
    3. 配置专用提示词（`AIConstant`）

- **集成新工具**
    1. 实现 `Tool` 接口
    2. 注入到目标聊天客户端中

---

## 📚 参考文档

- [Spring AI 官方文档](https://docs.spring.io/spring-ai/docs/)
- [Redis 官方文档](https://redis.io/documentation)
- [Ollama 模型部署](https://ollama.com/)

---

## 🧑‍💻 贡献指南

1. Fork 项目并创建新分支
2. 提交代码并附带单元测试
3. 提交 PR 并描述修改内容

---

## 📄 许可证

本项目采用 **Apache 2.0** 开源许可证。

---

**作者**：Lucius Wan
**仓库地址**：[GitHub](https://github.com/LuciusWan/SpringAI-Agent)

---
