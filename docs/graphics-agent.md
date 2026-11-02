# Text2Splat Agent（造物方舟）：第一版技术方案

## 目标与范围

用户可正常聊天、讨论和澄清设计；只有聊天主管原生调用 `generate_3d_model` MCP 工具后，才交给独立 3D Agent 生成。无需上传照片，默认使用免费模型 API 生成设计和图片，计算阶段复用本机 web-scan 的 COLMAP 与 Brush。真实重建验证通过才交付 3D 展品；概念图模式保留在兼容 API 中供调试。

第一版面向单个静态、不透明、带表面纹理的文创物件。透明玻璃、镜面金属、复杂镂空、大场景、动画和可制造精确网格不在本版本验收范围。Gaussian 表示不等于 CAD / STL，也不能承诺准确背面结构。

## 界面约束

主界面保持一个消息输入框和「发送」按钮，按需出现当前对话、生成进度、取消和模型结果，不增加模式切换、Agent 选择器、历史侧栏或技术面板。消息先 POST 聊天接口，正常回复不创建生成任务；生成过程中仍可聊天，等待本轮回复时才暂时禁用发送。同一会话有活动生成时拒绝重复委派。刷新对话或旧任务链接只恢复状态，不发起新的模型请求；旧概念图不能显示为 3D 成功。

## 组件与边界

| 组件 | 职责 | 数据 / 调用 |
| --- | --- | --- |
| 对话主管 ChatAgent | 通用问答、上下文、工具选择、委派 | chat.sqlite3、chat:{turnId} 检查点、LangChain ChatModel |
| 生成委派 MCP | 将有效生成请求交给 3D 专家 | 官方 MCP 内存传输；真实 initialize / tools/call |
| 3D 专家 Workflow | ReAct 生成循环、任务持久化、取消 | 独立工作队列/检查点、旧 JSON、LangChain MCP 工具 |
| Creative MCP | GLM 设计规格、CogView / Seedream 图片、GLM 视觉检查 | SQLite 生图队列、本地图片 |
| Web-scan MCP | 翻译为现有 web-scan API、幂等导入和训练提交 | 资产集合 ID → session ID → job ID |
| web-scan | 图片验证、SfM、Brush、本地导出 | 原有数据库、队列和 artifacts |
| web-scan viewer | 真实 Gaussian 渲染、旋转与下载 | `?p={sessionId}&j={jobId}` |

模型负责下一步行动选择、设计内容与外观判断。LangGraph 执行模型—工具循环，LangChain 绑定原生工具调用及文本/视觉模型。训练轮询由异步工作队列完成，不让大模型反复消耗 token 查询进度。MCP 只交换 ID、小型元数据和结果链接，不将 PLY 或大批图片塞进模型上下文。

## 多 Agent 委派与聊天

`chat.py` 的 LangGraph 主管能直接回复普通问题，也可通过 LangChain 原生 function calling 选择 `generate_3d_model`。提示词明确将提及 3D、否定请求、引用命令、咨询和头脑风暴视为聊天，要求生成意图不明确时澄清。代码不使用关键词匹配启动任务；只有完整、单个、白名单内且参数有效的原生调用才进入委派节点。语义意图由模型判断，尚未通过真实模型误触发率评估，不能宣称完全杜绝模型误判。

主管每消息一次模型请求，最多 45 秒、1024 输出 tokens，关闭 SDK 重试。异步委派成功即返回程序生成的受理回执，不再花一次模型请求改写，也不声称 3D 已完成。专家是独立的有状态 ReAct Agent，不只是一次生图函数；它自行选择后续图形工具，运行时与主管工作队列分离，用户可以继续聊天。不是任意多 Agent 自动组队，也不新增没有实际职责的角色。

`generation_mcp.py` 使用 FastMCP 与官方 `create_connected_server_and_client_session`，通过真实 MCP 消息的内存传输连接 ClientSession；没有新增 HTTP 端口。模型仅提供 description，请求标识由程序绑定。内部 MCP 另外支持 request_id、兼容接口的 parent_job_id，这些字段不暴露给模型。同一对话有活动任务时拒绝新委派。原 `/jobs` 的显式 THREE_D 请求也通过此工具，不从网页绕过它直接提交。

`chat_store.py` 原子保存会话、消息幂等键及委派意图。MCP 响应丢失时只按已记录标识查原任务，不重发；重启对尚未完成消息只标记中断或重新关联已记录任务，不重放模型调用和副作用。GET 会话/消息/请求接口均只读。聊天没有图形服务预检；仅在真正委派后由专家执行原有预检。健康接口仍可单独查询图形服务。

聊天上下文带最新真实任务状态、选中的长期记忆和最多 6 轮完整对话；较早内容是有限用户消息摘录。超出 12,000 个 JSON 字符预算时先减少记忆，再整体减少旧轮次/摘录，不截断当前消息与必要状态。原始会话保留在 SQLite；摘录不保证保留全部旧设计细节。明确偏好可通过聊天写入，来源记录为 source_turn_id；普通聊天不生成任务经验。前端以 textContent 展示模型文本，不执行 HTML。

## 3D 专家 ReAct 循环与检查点

`agent/text2splat/workflow.py` 的 StateGraph 为 `preflight → model ⇄ tools → finish`，不再通过固定边依次调用全部阶段。`react.py` 用 LangChain `bind_tools` 将工具 schema 提交给选定文本模型（默认 GLM-4.7-Flash）；模型产生原生 tool_calls，执行结果以对应 tool_call_id 的 ToolMessage 返回，驱动下一轮决策。它不依赖解析文本中的 Thought/Action，也不要求输出思维链。

模型工具为参考图、候选视图、导入、重建、验证、任务检查、完成和停止。前五个工具调用已有 MCP 服务，并在程序内等待结果；可选的 `design_hint` 只能作为原始描述的补充。工具参数不接受其他任务 ID、路径、费用开关或供应商配置；每次执行都检查当前任务的前置条件。

决策上限 12 轮，每轮输出上限 1024 tokens、等待上限 45 秒，每次只能选择一个工具；多调用批次不部分执行。代码记录有效操作意图，防止再次执行生成、导入、训练或验证。参数/阶段拒绝会返回观察，允许模型纠正；真实工具失败会先清理下游，后续只能检查失败或停止，保留 FAILED / NEEDS_REVIEW / CANCELLING 的原有语义。模型返回结束文本时仍检查证据，不能用声明替代真实 Gaussian PLY。

`thread_id` 使用原任务 ID，AsyncSqliteSaver 在节点边界同步持久化工具调用与观察；原 JSON 记录继续服务页面，并在每次生图或训练提交前记录请求意图。模型回复中的解释、供应商 reasoning_content 和元数据不会写入检查点。不同任务使用独立消息历史，只有经过选择的长期偏好与任务经验进入新任务上下文。

不在有副作用的节点配置 RetryPolicy，也不在服务重启时对未完成检查点直接调用 `ainvoke(None)`。已有意图只允许查询原结果；查询为空仍不重发提交。重启保留此前的中断清理行为。检查点与下游执行不是同一个事务，不能把该实现称为 exactly-once 或保证自动续跑。

LangChain 的模型适配器显式关闭 SDK 重试（`max_retries=0`），限定智谱 Chat Completions 端点；ReAct 只发送供应商支持的 `tool_choice=auto`，不附带 JSON 模式、strict 或 parallel_tool_calls。MCP 内的文本规划 JSON 模式与视觉提示词 JSON 解析继续分别处理。图片接口没有统一的 ChatModel 契约，因此沿用已有供应商图片适配器。MCP 工具采用固定白名单和参数 schema，解析既有 `ok/data/error` 信封；不启用 LangChain 原生 MCP beta 或迁移 MCP v2。

## 跨任务记忆与模型上下文

`memory.py` 是项目自有 SQLite 记忆库，不是 LangGraph BaseStore，也没有引入向量数据库。它与 LangGraph 的每任务检查点分开持久化，按本机数据目录隔离。

- 偏好：只接受记忆 API 明确写入，或描述中独立一行的「记住我的偏好：……」「请记住我喜欢……」及 `Remember that I prefer …` 等受支持格式。自然语言格式更新同一个 `explicit-description` 条目；不从普通物件描述推断用户画像。记录来源和时间，当前要求优先。
- 任务经验：完成或确有生成证据的失败记录自动写入，启动时幂等补入旧任务。记录结果类别、描述、标题、审核/失败原因和来源任务 ID；不复制产物地址、路径、API 配置或完整对话。概念图不标为 3D，READY 还须有已验证 Gaussian PLY 记录。
- 检索：每个新任务在开始时载入最多 4 条最新偏好，并在最近 300 条任务经验中用英文词项/中文双字重叠评分选出最多 3 条。没有匹配时不注入任务经验。其作用是相关历史参考，失败不代表已证明因果，历史成功不允许跳过当前步骤。
- 遗忘：单条删除或清空都会写遗忘标记，防止启动回填复活；明确的新偏好更新允许重新记住。删除不会抹除旧任务文件、检查点和已载入上下文，影响后续新任务。数据库异常不阻断生成，健康接口暴露可用状态；记忆管理 API 返回服务不可用。

`context.py` 只改变下一次模型请求的视图，不删 LangGraph 原始消息。任务消息超过 12,000 个 JSON 字符或 6 组完整交互时，使用当前程序状态与实际工具回执构建结构化摘要，优先保留最近 2 组交互。摘要带有已尝试操作、允许操作、验证状态与剩余决策数，不重置 12 轮预算。预算不含固定系统提示和工具 schema，不宣称是模型 token 上限。

原始用户消息保持完整。摘要和历史记忆作为明确标注的数据消息送入；先移除低优先级历史记忆，再移除整组旧交互，确保原生 tool_call_id 一一配对。若摘要不能缩短且原上下文仍在预算内则保留原上下文；若原始描述和必要任务事实仍超预算，则停止决策而不静默截断。任务 `memorySources` 记录检索候选来源，`contextStats` 记录最近模型调用的压缩情况和实际记忆数量；检查点另存摘要及统计用于核查。

本地检索与压缩不增加 LLM/embedding 请求，但选中的记忆内容会随正常决策请求发给已配置模型。未验证真实模型遵循记忆的效果或实际 token 节省量。管理 API 与示例见 README，不增加首页功能控件。

## MCP 工具契约

Creative 与 Web-scan 两个服务只监听 loopback，使用 `/mcp` Streamable HTTP；新增生成委派 MCP 仅在 Agent 进程内。均使用官方 SDK；底层图形工具由 LangChain StructuredTool 包装。统一返回 `{"ok":true,"data":...}` 或 `{"ok":false,"error":"可展示错误"}`。

| 工具 | 输入 | 返回 |
| --- | --- | --- |
| `generate_3d_model` | description、程序绑定的 request_id、可选 parent_job_id | 已受理的 3D 专家任务；不是已完成模型 |
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

1. `ZHIPU_TEXT_MODEL` 选定的模型（默认 `glm-4.7-flash`）输出固定规格 JSON：物件名称、形状、材质、纹理与结构。
2. `cogview-3-flash` 根据规格生成一张参考图。概念模式在此完成。
3. 3D 模式按同一规格逐张请求 12 个角度。每张保存后按归一化 RGB 像素摘要与已有候选图比较，完全重复即终止为 `needs_review`，指出重复的两张图、保留已有图片，不调用剩余生图或外观审核，不发布可导入集合、不自动补图。仅调整编码或 PNG 元数据不能绕过检查；此处不判断近似重复或几何一致性。CogView 不接收参考图，因此完成的清单明确记录 `reference_conditioned=false`、`geometry_verified=false`。
4. `glm-4.6v-flash` 接收缩略参考图和候选视图，检查明显结构差异、拼图和视角变化。格式异常或判断不一致时，不开始重建。
5. web-scan 再次验证不同照片数量与像素数据，冻结 manifest。COLMAP 估计相机，Brush 用实际估计结果训练。
6. 真实训练成功且存在非空 Gaussian PLY 后，Agent 才返回 `READY`。

智谱的 `response_format` 参数仅用于文本模型：设计规划启用 JSON 模式；视觉审核通过提示词要求 JSON，再在本地解析并检查 `consistent` 布尔值和 `reason` 字符串。空回复、非 JSON、截断或字段异常均停止流程，不发布可重建集合，不自动重试审核。视觉接口不携带仅文本模型支持的参数；这不代表审核已经通过真实模型或几何验证。

可选 `IMAGE_PROVIDER=seedream` 将参考图输入每次视图生成请求。必须同时配置 `ARK_API_KEY` 与 `ALLOW_PAID_IMAGE_API=true`。不自动重试、不自动切换供应商，每次完整尝试最多 13 个生图请求。参考图约束仍不能保证跨图三维一致性。

## 任务可靠性

- 聊天前端使用 `Idempotency-Key`，相同 key/内容/对话复用消息，冲突返回 400。发送前写入 `sessionStorage` 与 `?chat_request=`；受理后改为 `?chat=`，恢复只调用 GET 消息/会话接口。未确认消息不能被另一条发送悄悄替换；旧生成请求记录仍只读恢复。
- 主管和 3D 专家各有异步工作队列，各最多 16 个待处理项；同一对话同时仅一个待回复消息，但与生成并行。Creative 单工作线程，最多 32 个活动任务。
- 生成提交前写入请求标识。响应丢失时按标识查询并清理遗留任务，不重新请求图片。
- Agent 任务文件原子替换并兼容旧 Java JSON；LangGraph 节点状态写入独立 SQLite 检查点；MCP SQLite WAL 记录幂等映射。每个服务使用进程文件锁避免重复启动。
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

已通过用户提供的本地密钥验证 GLM-5.3-Flash 的聊天、生成意图原生工具选择、只读 ReAct 工具结果往返和设计 JSON。**尚未验证：真实外部生图、生成图片的多视角几何一致性、从生成图到可旋转 3D 的最终质量。** 不能以文本模型调用、单元测试或已有真实照片重建结果替代这项验证。

真实验收表：

| 样例 | 参考图符合描述 | 12 张不同视图 | 外观检查 | SfM 注册 / 输入数量 | PLY 与旋转观感 |
| --- | --- | --- | --- | --- | --- |
| 哑光卷草纹花瓶 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |
| 青瓷小鸟香插 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |
| 熊猫木雕摆件 | 待验证 | 待验证 | 待验证 | 待验证 | 待验证 |

若免费路径持续无法通过 SfM，后续应评估参考图条件模型或真正的多视角生成模型，不能通过放宽几何门槛、虚构相机外参或改用 STL 来宣称完成。

## ReAct 运行与验证边界

Agent 和 Creative MCP 都从本地 `.env` 读取同一个 `ZHIPU_API_KEY`，浏览器不接触密钥。健康接口返回决策模型配置状态和轮数上限；任务 API 记录 `agentLoop`、`agentRounds`、`agentActions`。正常 3D 流程约有 6 次决策模型请求，检查与纠错可能增加至 12 次，原有设计规划和视觉审核另计。取消和总超时在等待模型时也会检查，停止等待不代表供应商撤销了已收到的请求。

现有证据包括原生 function calling 的 HTTP 契约替身、真实 LangGraph 循环和有限真实 API 样例；这些样例不是完整的工具选择质量或误触发率评估。ReAct 不会让独立文生图自动具备多视角几何一致性，也不绕过真实重建门槛。

文本模型由 `ZHIPU_TEXT_MODEL` 显式选择，支持默认免费的 `glm-4.7-flash` 与计量的 `glm-5.3-flash`；不自动降级或换模型。资源包只代表对应模型的可用额度，不保证持续免费，当前不读取控制台余额。该配置共享给聊天、专家决策与设计规划，不改变 CogView、视觉审核或付费生图开关。

GLM-5.3-Flash 必须开启思考，使用 `thinking={type: enabled, clear_thinking: true}` 和 `reasoning_effort=low`。固定版本 langchain-openai 会丢弃供应商的 reasoning_content，故以小型 ZhipuChatModel 子类在请求/响应边界处理它：通过程序生成的消息 ID 关联，只在当前任务内存中保留并原样回传完整工具交互；供应商未返回时保持缺省。消息、检查点与记忆没有此字段；上下文压缩同步丢弃对应缓存，任务完成、失败和取消均清除。已经丢失的临时上下文不能用虚构内容恢复，服务重启仍不自动重放任务。这些供应商字段不计入可见消息的 12,000 字符预算，仍受单次输出上限与最多 12 轮限制。

## 官方接口参考

- [智谱模型价格](https://docs.bigmodel.cn/cn/guide/start/pricing)
- [智谱图像生成 API](https://docs.bigmodel.cn/api-reference/模型-api/图像生成)
- [Seedream 图像生成](https://docs.volcengine.com/docs/ark/image-generation-tutorial?lang=zh)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [LangGraph 持久化与检查点](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain MCP 文档](https://docs.langchain.com/oss/python/langchain/mcp)

- [ReAct 论文](https://arxiv.org/abs/2210.03629)
- [LangChain 记忆概念](https://docs.langchain.com/oss/python/concepts/memory)
- [LangChain 短期记忆与上下文管理](https://docs.langchain.com/oss/python/langchain/short-term-memory)
- [LangChain Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)
- [MCP Python SDK v1.30 内存传输源码](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/src/mcp/shared/memory.py)
- [LangGraph Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [GLM-4.7-Flash 工具调用能力](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash)
- [智谱对话补全参数](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)
