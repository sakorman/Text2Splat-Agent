# Text2Splat Agent（造物方舟）：第一版技术方案

## 目标与范围

输入一句物件描述，无需上传照片。默认使用免费模型 API 生成设计和图片，计算阶段复用本机 web-scan 的 COLMAP 与 Brush。用户通过一个输入框和一个「生成 3D 模型」按钮启动完整流程，交付通过真实重建后才能打开的 3D 展品。参考图与候选视图是后台中间产物，概念图模式仅保留在 API 中供调试。

第一版面向单个静态、不透明、带表面纹理的文创物件。透明玻璃、镜面金属、复杂镂空、大场景、动画和可制造精确网格不在本版本验收范围。Gaussian 表示不等于 CAD / STL，也不能承诺准确背面结构。

## 界面约束

主界面只保留描述输入和生成按钮，不增加模式切换、历史列表、示例按钮、服务状态面板或多视图缩略图。生成期间显示单条阶段状态和取消操作，完成后显示模型预览与下载。提交固定使用 `THREE_D`，生成期间禁用重复提交；刷新带任务 ID 的地址恢复进度，不发起新的模型请求。失败时保留描述和可操作的原因；旧概念图任务不能显示为 3D 成功。后续定时优化应保持这个单一入口。

## 组件与边界

| 组件 | 职责 | 数据 / 调用 |
| --- | --- | --- |
| Spring Boot Agent | 持久化任务、驱动阶段、取消、SSE、前端 | 本地 JSON 任务记录，MCP 客户端 |
| Creative MCP | GLM 设计规格、CogView / Seedream 图片、GLM 视觉检查 | SQLite 生图队列、本地图片 |
| Web-scan MCP | 翻译为现有 web-scan API、幂等导入和训练提交 | 资产集合 ID → session ID → job ID |
| web-scan | 图片验证、SfM、Brush、本地导出 | 原有数据库、队列和 artifacts |
| web-scan viewer | 真实 Gaussian 渲染、旋转与下载 | `?p={sessionId}&j={jobId}` |

模型负责设计内容与外观判断，Java 使用可预测的工作流调用内置 MCP。训练轮询由后台线程完成，不让大模型反复消耗 token 查询进度。MCP 只交换 ID、小型元数据和结果链接，不将 PLY 或大批图片塞进模型上下文。

## MCP 工具契约

两个服务都只监听 loopback，使用 `/mcp` Streamable HTTP、官方 Python SDK FastMCP 与 Java SDK。工具统一返回 `{"ok":true,"data":...}` 或 `{"ok":false,"error":"可展示错误"}`。

| 工具 | 输入 | 返回 |
| --- | --- | --- |
| `creative_health` | 无 | 模型、配置状态，不返回密钥 |
| `creative_generate_reference` | `prompt`, `request_id` | 立即返回生图任务 ID |
| `creative_generate_multiview` | `reference_id`, `request_id` | 立即返回多视图任务 ID |
| `creative_get_job` | `job_id` | 状态、资产 URL、设计与检查结果 |
| `creative_get_request` | `request_id` | 从幂等记录恢复任务 ID，不重新生成 |
| `creative_cancel_job` | `job_id` | 停止后续生成步骤 |
| `webscan_health` | 无 | 本机服务与 Brush 可用性 |
| `webscan_import_generated_views` | `asset_set_id` | 已冻结的 `session_id` |
| `webscan_start_reconstruction` | `session_id`, `request_id` | `job_id` |
| `webscan_get_status` | `job_id` | 真实阶段、进度和报告 |
| `webscan_get_request` | `request_id` | 恢复可能丢失响应的训练任务 ID |
| `webscan_get_result` | `job_id` | 有效 Gaussian PLY、viewer 与其他产物链接 |
| `webscan_cancel_reconstruction` | `job_id` | 下游取消状态 |

## 免费路径与可选生图路径

1. `glm-4.7-flash` 输出固定规格 JSON：物件名称、形状、材质、纹理与结构。
2. `cogview-3-flash` 根据规格生成一张参考图。概念模式在此完成。
3. 3D 模式按同一规格逐张请求 12 个角度。每张保存后按归一化 RGB 像素摘要与已有候选图比较，完全重复即终止为 `needs_review`，指出重复的两张图、保留已有图片，不调用剩余生图或外观审核，不发布可导入集合、不自动补图。仅调整编码或 PNG 元数据不能绕过检查；此处不判断近似重复或几何一致性。CogView 不接收参考图，因此完成的清单明确记录 `reference_conditioned=false`、`geometry_verified=false`。
4. `glm-4.6v-flash` 接收缩略参考图和候选视图，检查明显结构差异、拼图和视角变化。格式异常或判断不一致时，不开始重建。
5. web-scan 再次验证不同照片数量与像素数据，冻结 manifest。COLMAP 估计相机，Brush 用实际估计结果训练。
6. 真实训练成功且存在非空 Gaussian PLY 后，Agent 才返回 `READY`。

可选 `IMAGE_PROVIDER=seedream` 将参考图输入每次视图生成请求。必须同时配置 `ARK_API_KEY` 与 `ALLOW_PAID_IMAGE_API=true`。不自动重试、不自动切换供应商，每次完整尝试最多 13 个生图请求。参考图约束仍不能保证跨图三维一致性。

## 任务可靠性

- 前端使用 `Idempotency-Key`；相同 key 和内容复用任务，内容冲突返回 400。提交前把原请求写入标签页 `sessionStorage` 并设置 `?request=` 地址；响应丢失后刷新只调用 `GET /api/graphics/requests/{key}` 找回任务，未找到时保留标识供手动重试，不自动生成。确认任务 ID 后清理提交记录。
- Java 单工作线程，最多 16 个活动任务；Creative 单工作线程，最多 32 个活动任务。
- 生成提交前写入请求标识。响应丢失时按标识查询并清理遗留任务，不重新请求图片。
- Java 任务文件原子替换；MCP SQLite WAL 记录幂等映射。每个服务使用进程文件锁避免重复启动。
- 下游导入按资产集合复用采集；web-scan 本身进一步去重。训练启动前保存采集 ID 与完整预设；已有启动意图的同一请求只做查询，不重发训练 POST。响应丢失时必须唯一匹配采集与参数，确认后持久化任务 ID；零个或多个匹配均保留未知状态，不能取列表首项作为取消目标。启动响应自身也核对采集与参数，发现其他参数的活动任务时不发送新训练请求。旧意图按此前固定预设兼容查询。
- 取消优先于晚到的成功结果。已发送的外部模型请求不能撤回，重建取消交给 web-scan 终止进程组。
- 浏览器只接收属于当前任务、且与原请求任务 ID 相符的异步更新。终态保持不变，`CANCELLING` 不被旧的运行进度覆盖；取消请求的等待状态按任务 ID 记录。旧请求返回或失败不能污染新任务的界面，旧请求的结束处理也不能提前启用新任务仍在等待的取消按钮。
- 服务重启不自动生成新图片或续训；中断任务被标记并清理。旧图片和报告保留用于判断问题。
- 最长等待默认 2 小时，超时尝试停止下游任务；停止无法确认时显示 `CANCELLING`，支持恢复服务后再次取消。
- 仅 `creative_get_job`、`webscan_get_status`、`webscan_get_result` 在明确的临时连接故障时自动重试，每次查询最多 3 次；退避按轮询间隔指数增长并封顶 5 秒，默认等待 2 秒、4 秒。网络错误或适配器明确的 `TEMPORARY_UNAVAILABLE` 才进入这一路径；协议、参数、格式与质量错误直接交给原有失败处理。写操作和清理不自动重放。
- 退避期间每至多 100 毫秒检查取消与总超时，并在每次只读调用前后检查；单次 MCP 请求仍最多等待既有 90 秒，不能把总超时描述为对在途请求的即时中断。恢复中通过现有进度行显示提示，正常返回后清除 `connectionRetry` 状态。
- 不把模型输出当作路径、URL、Shell 命令或相机参数。下载地址限公网 HTTPS，限定大小，图片经 Pillow 验证并重新编码。

## 验收与当前证据

自动验证覆盖独立启动、API/SSE、真实 MCP SDK、接口契约、参考图完成、视图不一致、重复视图、重建失败、取消竞态、幂等冲突、重启与缺失产物。测试中的图片和模型返回仅为测试替身，不进入产品成功兜底逻辑。

在目标 Mac 上已确认 web-scan `/api/health` 报告 `brush_available=true`，Agent 能通过两个真实 MCP 服务读取配置与本机重建状态。

**尚未验证：真实外部模型生成、生成图片的多视角几何一致性、从生成图到可旋转 3D 的最终质量。** 原因是此环境未配置个人智谱 API Key。不能以单元测试或已有真实照片重建结果替代这项验证。

真实验收表：

| 样例 | 参考图符合描述 | 12 张不同视图 | 外观检查 | SfM 注册 / 输入数量 | PLY 与旋转观感 |
| --- | --- | --- | --- | --- | --- |
| 哑光卷草纹花瓶 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |
| 青瓷小鸟香插 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |
| 熊猫木雕摆件 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |

若免费路径持续无法通过 SfM，后续应评估参考图条件模型或真正的多视角生成模型，不能通过放宽几何门槛、虚构相机外参或改用 STL 来宣称完成。

## 官方接口参考

- [智谱模型价格](https://docs.bigmodel.cn/cn/guide/start/pricing)
- [智谱图像生成 API](https://docs.bigmodel.cn/api-reference/模型-api/图像生成)
- [Seedream 图像生成](https://docs.volcengine.com/docs/ark/image-generation-tutorial?lang=zh)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Java SDK](https://github.com/modelcontextprotocol/java-sdk)
- [Spring AI 1.1.8](https://github.com/spring-projects/spring-ai/tree/v1.1.8)
