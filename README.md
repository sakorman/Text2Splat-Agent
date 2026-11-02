# Text2Splat Agent · 造物方舟

**基于 LangGraph + LangChain 的本地 3DGS Agent。** Text 表示文字描述，Splat 对应 3D Gaussian Splatting；通过内置 MCP 串联设计、生图与本机重建。

一句话设计一个文创物件：生成参考图、尝试生成 12 张候选视图，在自己的电脑上运行 COLMAP + Brush，得到可旋转查看的 3D Gaussian 展品。

**这是第一版可运行原型。** 默认选用智谱免费模型；免费文生图不具备可靠的参考图约束，多视角一致性与最终重建效果仍需真实样例验证。系统只在 web-scan 实际重建成功且存在 Gaussian PLY 后显示“3D 已完成”，不会用假模型或 STL 代替结果。

## 第一版包含什么

- 极简生成页 `/graphics/`：一个描述输入框、一个「生成 3D 模型」按钮。自动执行完整流程，生成时显示进度并允许取消，成功后显示模型预览和下载；失败可修改描述重试，刷新当前任务地址可恢复进度。
- 两个内置 MCP 服务，使用官方 MCP SDK 的 Streamable HTTP 协议。
- 默认免费模型：`glm-4.7-flash` 做 ReAct 决策与整理设计、`cogview-3-flash` 生图、`glm-4.6v-flash` 检查外观。
- 可选 Seedream 4.0 参考图约束生成，必须通过本地配置显式开启付费，不自动降级到付费服务。
- 接入已有 [web-scan](https://github.com/sakorman/web-scan)：导入图片、冻结采集、SfM、Brush 本地 GPU 训练、预览与下载。
- 异步任务、本地持久化、请求幂等、取消传播、重启中断恢复、丢失提交响应后的任务查询；进度连接短暂中断时有限重试只读查询。
- Python LangGraph 执行 ReAct 模型—工具循环，SQLite 保存消息和节点检查点；LangChain 接入规划、视觉模型与 MCP 工具。FastAPI 提供原有 API/SSE 和页面。
- 3D Agent 不依赖 Java、Maven、MySQL、Redis、Ollama；原 Spring AI 聊天模块保留为历史独立应用。

## 快速启动

需要 **Python 3.11+**。macOS / Linux 支持启动脚本；本地 3D 还需要已安装的 web-scan 和 Brush。Apple Silicon 可使用 Brush 的 Metal 后端，不需要 NVIDIA 显卡或云端 GPU。

```bash
git clone https://github.com/sakorman/Text2Splat-Agent.git
cd Text2Splat-Agent
python3.12 -m venv .venv
.venv/bin/python -m pip install -e 'mcp[dev]' -e 'agent[dev]'
cp .env.example .env
```

编辑本地 `.env`，填写自己在 [智谱开放平台](https://bigmodel.cn/) 创建的 `ZHIPU_API_KEY`。密钥由后端 Agent 决策模型与 Creative MCP 使用，页面不接收密钥，`.env` 已被 Git 忽略。

```dotenv
ZHIPU_API_KEY=填写自己的密钥
IMAGE_PROVIDER=cogview
ALLOW_PAID_IMAGE_API=false
WEBSCAN_REPO=../web-scan
WEBSCAN_URL=http://127.0.0.1:8787
```

```bash
./scripts/start-graphics.sh
```

打开 **http://127.0.0.1:8080/graphics/**。脚本启动两个 Python MCP 服务和 FastAPI/LangGraph Agent；检测到本机 web-scan 已运行时直接复用，否则尝试用 `WEBSCAN_REPO/.venv/bin/python` 启动它。Ctrl-C 只停止本次脚本启动的进程。修改 Python 代码后需重启服务；不会因开发热重载自动中断或重复执行生成。

输入物件描述，点击「生成 3D 模型」。页面始终提交 `THREE_D` 任务，后台在生图前检查 web-scan、Brush 和模型密钥。中间图片、技术配置和历史列表不出现在主界面；旧任务和图片仍保存在本地，概念图模式保留为 API 调试能力。

免费资格、限速和费用以供应商当前规则为准，本项目不提供免费额度，也不能保证供应商永远免费。

### 单独启动各服务

在仓库根目录执行；Agent 与 MCP 都自动读取 `.env`。

```bash
.venv/bin/graphics-mcp creative --port 8766
.venv/bin/graphics-mcp webscan --port 8767
.venv/bin/text2splat-agent
```

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| Agent / 工作台 | `127.0.0.1:8080` | 任务 API、页面和本地图片 |
| Creative MCP | `127.0.0.1:8766/mcp` | 设计、生图、外观检查 |
| Web-scan MCP | `127.0.0.1:8767/mcp` | 包装已有本地重建 API |
| Web-scan | `127.0.0.1:8787` | COLMAP、Brush、3D viewer 和产物 |

所有服务面向本机单用户使用。不要直接公开到互联网；第一版没有多用户鉴权。页面与 Agent 同源，不需要单独部署前端仓库。

## Agent 框架与迁移

当前版本固定使用 LangGraph **1.2.19**、LangChain **1.4.7**、langchain-openai **1.7.3** 和 SQLite checkpointer **3.1.3**。`langchain-openai` 在这里是智谱兼容接口适配器，不会改用 OpenAI 模型或启用新的收费服务。生图仍使用供应商专用 HTTP 接口。

主循环采用 **ReAct**：`preflight → model ⇄ tools → finish`。`glm-4.7-flash` 通过 LangChain 原生 function calling 选择行动，工具结果作为 ToolMessage 返回模型，再由模型决定继续、检查任务或停止。模型可为原始描述补充渲染建议；任务 ID、供应商、训练参数和结果地址由程序绑定。

模型可调用 `generate_reference`、`generate_multiview`、`import_views`、`reconstruct`、`validate_result`、`inspect_task`、`finish`、`stop`。生成工具封装已有 MCP 提交与等待；LangChain StructuredTool 仍通过官方 MCP SDK 执行底层调用。真实依赖仍由代码检查：只有参考图完成才能生成候选视图，审核通过才能导入，真实重建与产物验证通过才能完成。

每个任务最多 **12 轮决策**，单次决策最多等待 45 秒、最多输出 1024 tokens，不自动重试模型请求。正常 3D 路径需要约 6 次决策调用，另有原来的设计规划和视觉审核；轮询不会消耗额外模型 token。每轮只执行一个工具，多工具批次整批拒绝；生成、导入、重建和验证各最多执行一次。实际工具失败后先清理下游，再让模型观察失败并检查或停止，不自动重新生图或续训。工具参数错误可在剩余决策预算内纠正。

`finish` 和模型的最终文本都不能绕过证据检查：尚未验证产物就结束会得到失败状态，不能显示 3D 成功。页面不展示思维链；检查点保存工具调用和观察，API 提供 `agentLoop`、`agentRounds`、`agentActions`，健康接口报告 `agent_loop=react`。

升级前停止旧 Java Agent 与旧 MCP，保留 `.graphics/`，安装上述依赖后重新启动。原任务地址、请求标识、图片和结果继续可读；未完成任务按旧规则查询并清理下游，不自动重发生图或续训。SQLite 检查点记录节点状态，但不提供未经副作用核对的自动断点重放；当前未开放手动 replay API。

## 长期记忆与上下文压缩

不同任务保留各自完整消息历史，并通过本地 `memory.sqlite3` 共享选中的历史经验。任务结束后记录描述、结果类别和失败/审核原因；明确区分概念图、待审核、失败与经过产物验证的 3D。启动时补入旧任务记录，跳过取消和没有生成证据的配置失败。新任务从最近 300 条任务记忆中按中英文词项匹配检索最多 3 条相关经验，另带最多 4 条最新的明确偏好；这是轻量词项检索，不是向量语义搜索。

只在你明确要求时记录长期偏好。例如在同一个描述框中输入：

```text
记住我的偏好：哑光青瓷。
生成一个卷草纹花瓶
```

也识别独立一行的「请记住我喜欢……」和 `Remember that I prefer …`。描述中的普通材质形容词不会自动成为个人偏好。这类自然语言记录更新同一条偏好；命名偏好可通过下表 API 单独维护。当前描述优先，历史结果不会替代本次生成、审核或产物验证。

ReAct 每轮对任务上下文做预算检查：超过 **12,000 个 JSON 字符**或 6 组完整工具交互时，用程序根据实际任务状态和工具回执整理的摘要压缩旧记录，优先保留最近 2 组完整交互。压力较大时先移除低优先级记忆，再整体移除旧交互；不会拆散工具调用和结果。原始描述、当前约束和剩余轮数不丢弃，无法容纳时明确停止。短上下文或不能缩短的摘要不强制压缩。这个预算不包含固定系统提示与工具定义，也不是精确 token 数；原始消息仍保存在检查点中。

记忆检索和摘要不额外调用模型或 embedding API；实际送入决策上下文的记忆会随正常请求发给已配置的决策模型。任务 API 中 `memorySources` 列出检索来源，`contextStats.memoryItems` 表示最近一轮实际注入数量，其他统计展示压缩前后字符数。记忆库不可用时生成继续运行，健康接口 `memory.available=false`。

可通过 API 查看、更新、删除单条或清空现有记忆。删除项有遗忘标记，重启不会从旧任务重新补回；之后明确更新同一偏好可以重新记住。删除只影响后续新任务检索，不删除原任务、图片、检查点或已运行任务载入的上下文。记忆按 `GRAPHICS_DATA_DIR` 隔离，当前仍为本机单用户服务。首页没有新增记忆或配置面板。

## 模型与重建路径

```text
用户描述 → GLM 设计规格 → 参考图 → 12 张候选视图 → 外观检查
  → web-scan 导入 / 冻结 → COLMAP 估计真实相机 → Brush 本机训练
  → Gaussian PLY / Viewer
```

**默认免费路径：** CogView-3-Flash 只接收文字，12 张视图复用同一设计描述，逐张改变请求视角。这些是实验性候选图，不是严格一致的多视角数据。外观检查不能证明几何正确，COLMAP 仍可能失败。不会把提示词中的角度当成相机外参，也不会把一张拼图切成“12 张真实视角”。

每张候选图保存后立即检查归一化像素是否与已有候选图完全重复。一旦发现重复，保留已生成图片并说明重复位置，停止剩余生图和外观审核，不开始重建，也不自动补图。例如第 2 张重复时只请求 2 张候选图，避免继续请求剩下 10 张。像素不同不代表视角或几何一致，仍须通过后续检查。

**可选付费路径：** 在 `.env` 中设置 `IMAGE_PROVIDER=seedream`、`ARK_API_KEY` 和 `ALLOW_PAID_IMAGE_API=true`。每张候选视图都输入参考图，仍需外观检查与 SfM。每个完整 3D 尝试最多提交 1 张参考图 + 12 张候选视图，不自动重试生图；按实际生成数量由供应商计费。网络超时不代表供应商没有完成请求。

**本地训练固定快速预设：** `backend=brush`、`steps=3000`、`resolution=512`、`max_points=150000`、`camera_mode=auto`、`evaluation_mode=all`。生成图至少 12 张且不重复，实际入库、匹配与几何质量门槛继续由 web-scan 检查。`all` 使用全部输入进行训练，不是独立测试集质量证明。

成功结果链接复用 web-scan 的 viewer，保留完整 Gaussian PLY；`.splat` 为兼容格式，可能不保留全部视角相关颜色信息。

## API

```bash
curl http://127.0.0.1:8080/api/graphics/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: my-first-vase' \
  -d '{"prompt":"一个青瓷卷草纹花瓶，哑光表面","mode":"THREE_D"}'
```

页面在提交前将请求标识与描述保存在当前标签页的 `sessionStorage`。即使服务器接受任务后响应丢失，刷新也只查询原任务，不自动重发生成；尚未找到时，手动重试相同描述会复用原标识。确认任务 ID 后清除待确认记录。浏览器存储不可用时停止提交，避免产生无法恢复的重复请求。

接口立即返回 `202` 和任务 ID。同一 `Idempotency-Key` 与相同请求返回同一个任务；变更描述请使用新 key。`parentJobId` 可记录版本关系，新版本会重新生成参考图。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/graphics/health` | MCP 配置及本地 Brush 状态，不返回密钥 |
| POST | `/api/graphics/jobs` | `prompt`、`mode`（`CONCEPT` / `THREE_D`）、可选 `parentJobId` |
| GET | `/api/graphics/jobs` | 最近 50 个任务 |
| GET | `/api/graphics/jobs/{id}` | 完整状态与结果 |
| GET | `/api/graphics/requests/{key}` | 按原提交标识只读恢复任务；未找到返回 404，不启动生成 |
| GET | `/api/graphics/jobs/{id}/events` | SSE，事件名 `progress`，每次发送完整状态 |
| POST | `/api/graphics/jobs/{id}/cancel` | 停止后续步骤并取消下游任务 |
| GET | `/api/graphics/assets/{setId}/{name}` | 本地生成的 PNG，仅允许受控 ID 与文件名 |
| GET | `/api/graphics/memories?limit=50&offset=0` | 分页查看记忆，limit 为 1–100 |
| PUT | `/api/graphics/memories/preferences/{key}` | 明确保存偏好，JSON `{"value":"哑光青瓷"}`；内容 2–200 字，key 为 1–64 个小写字母、数字、下划线或短横线 |
| DELETE | `/api/graphics/memories/{id}` | 删除指定 `pref:…` 或 `episode:…` 记忆 |
| DELETE | `/api/graphics/memories` | 清空现有记忆，不删除任务与产物 |

状态包括 `QUEUED / PLANNING / REFERENCE / MULTIVIEW / IMPORTING / RECONSTRUCTING / VALIDATING / READY`，以及 `CONCEPT_READY / NEEDS_REVIEW / FAILED / CANCELLING / CANCELLED`。

生成进度、重建进度和产物查询遇到明确的临时连接故障时，每次查询最多尝试 3 次，默认等待 2 秒、4 秒后重试。不会重发生图、图片导入、训练启动或取消请求，也不会重试参数错误、无效返回内容或质量检查失败。退避期间可以取消，并检查原任务总超时；单次 MCP 调用仍受既有 90 秒请求超时限制。

页面按任务 ID 接收进度和取消结果。已经结束的任务不会被晚到响应改回处理中；开始新任务后，旧任务的取消响应或错误不会覆盖新进度，也不会改变新任务取消按钮的等待状态。

重建启动前保存采集 ID 和训练参数。启动响应丢失后，同一请求只查询已有任务，不再发送训练 POST；仅有一个采集与参数均匹配的任务时才保存关联。没有匹配或存在多个匹配时保留“尚未确认”状态，不按列表顺序选择取消目标。

取消无法撤回已发送给供应商的生成请求；任务不会在晚到的结果抵达后变成成功。下游停止结果无法确认时保留 `CANCELLING`，恢复服务后可再次取消。重启会查询并取消遗留任务，不自动续训或重发生成请求。

## 内置 MCP 与数据

工具表、调用约束、部署结构和验收清单见 [技术方案](docs/graphics-agent.md)。实现都在本仓库，**没有修改 web-scan 仓库**，它通过现有 HTTP API 被复用。

```text
agent/text2splat/             ReAct 模型/工具循环、LangChain 工具、FastAPI
agent/text2splat/static/graphics/  无构建依赖的工作台
agent/tests/                  工作流、检查点、API 与真实 MCP SDK 测试
mcp/graphics_mcp/             Creative / Web-scan MCP
scripts/                     本地启动与进程管理
.graphics/                   本地工作数据（忽略提交）
  workflows/                 原子写入的任务记录（兼容旧 Java JSON）
  assets/                    生成图片及清单
  mcp.sqlite3                生图任务、幂等与 web-scan 映射
  langgraph.sqlite3          LangGraph 消息/节点检查点（thread_id = 任务 ID）
  memory.sqlite3             跨任务偏好、经验与遗忘标记
```

web-scan 自己的数据库、训练缓存和 Gaussian 模型仍保存在它的数据目录。通过 `GRAPHICS_DATA_DIR` 修改 Agent 数据目录时，需要让 Agent 与两个 MCP 使用相同路径。不要同时启动多个 Agent 或同种 MCP 操作同一数据目录。

## 验证

```bash
.venv/bin/python -m pytest -q
node --check agent/text2splat/static/graphics/studio.js
node --test tests/*.test.mjs
```

自动测试不调用外部模型，不消耗 token，也不启动 GPU 训练。覆盖任务状态、取消竞态、重启处理、丢失响应、幂等、外观检查失败、SfM 失败、产物缺失、HTTP 导入契约、MCP SDK 工具声明与 API 输入校验，以及记忆检索/删除/重启、压缩预算和工具交互完整性。

真实验收仍需配置个人 Key，分别尝试花瓶、香插、摆件，检查 12 张图、SfM 注册数量、PLY 和实际旋转观感。当前版本不宣称已完成真实生图到 3D 的质量验收。

旧 RAG 测试需要 Redis、模型服务和 PDF，默认不运行。显式设置 `RUN_RAG_INTEGRATION=true` 与 `RAG_TEST_PDF` 后可运行。原聊天模块说明保存在 [历史文档](docs/legacy-chat.md)；该历史模块使用 Spring Boot **3.5.16** / Spring AI **1.1.8**、Java **17**，可独立运行 `mvn verify`；它不再参与 3D Agent 执行。

原仓库明文 API 配置已替换为环境变量。若原密钥曾有效，应在供应商后台撤销或轮换；删除当前文件中的值不会清除 Git 历史。

运行原聊天模块需 JDK 17+ 和 Maven，并配置 `CHAT_API_KEY`、`CHAT_BASE_URL`、`CHAT_MODEL`、`MYSQL_URL`、`MYSQL_USER`、`MYSQL_PASSWORD`、`REDIS_HOST`、`REDIS_PORT`。旧模块仍依赖相应数据库和模型服务。
