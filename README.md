# SpringAI-Agent · 造物间

一句话设计一个文创物件：生成参考图、尝试生成 12 张候选视图，在自己的电脑上运行 COLMAP + Brush，得到可旋转查看的 3D Gaussian 展品。

**这是第一版可运行原型。** 默认选用智谱免费模型；免费文生图不具备可靠的参考图约束，多视角一致性与最终重建效果仍需真实样例验证。系统只在 web-scan 实际重建成功且存在 Gaussian PLY 后显示“3D 已完成”，不会用假模型或 STL 代替结果。

## 第一版包含什么

- 中文工作台 `/graphics/`：物件描述、概念图 / 3D 两种模式、示例描述、实时进度、历史记录、图片预览、失败提示、取消与创建新版本。
- 两个内置 MCP 服务，使用官方 MCP SDK 的 Streamable HTTP 协议。
- 默认免费模型：`glm-4.7-flash` 整理设计、`cogview-3-flash` 生图、`glm-4.6v-flash` 检查外观。
- 可选 Seedream 4.0 参考图约束生成，必须通过本地配置显式开启付费，不自动降级到付费服务。
- 接入已有 [web-scan](https://github.com/sakorman/web-scan)：导入图片、冻结采集、SfM、Brush 本地 GPU 训练、预览与下载。
- 异步任务、本地持久化、请求幂等、取消传播、重启中断恢复、丢失提交响应后的任务查询。
- 新的 `graphics` 启动模式不依赖 MySQL、Redis、Ollama；原聊天模块仍可使用默认模式运行。

## 快速启动

需要 **JDK 17+、Maven、Python 3.11+**。macOS / Linux 支持启动脚本；本地 3D 还需要已安装的 web-scan 和 Brush。Apple Silicon 可使用 Brush 的 Metal 后端，不需要 NVIDIA 显卡或云端 GPU。

```bash
git clone https://github.com/sakorman/SpringAI-Agent.git
cd SpringAI-Agent
python3.12 -m venv .venv
.venv/bin/python -m pip install -e 'mcp[dev]'
cp .env.example .env
```

编辑本地 `.env`，填写自己在 [智谱开放平台](https://bigmodel.cn/) 创建的 `ZHIPU_API_KEY`。密钥只由后端 MCP 使用，页面不接收密钥，`.env` 已被 Git 忽略。

```dotenv
ZHIPU_API_KEY=填写自己的密钥
IMAGE_PROVIDER=cogview
ALLOW_PAID_IMAGE_API=false
WEBSCAN_REPO=../web-scan
WEBSCAN_URL=http://127.0.0.1:8787
```

```bash
# 按需设置 JAVA_HOME 指向 JDK 17 或更高版本
./scripts/start-graphics.sh
```

打开 **http://127.0.0.1:8080/graphics/**。脚本先构建 Java，然后启动两个 MCP 和 Agent；检测到本机 web-scan 已运行时直接复用，否则尝试用 `WEBSCAN_REPO/.venv/bin/python` 启动它。Ctrl-C 只停止本次脚本启动的进程。

未安装 web-scan 也可以使用“先看概念图”。“尝试生成 3D”会在生图前检查 web-scan 与 Brush 是否可用。

首次启动建议先生成概念图，确认账号可调用模型，再尝试 3D。免费资格、限速和费用以供应商当前规则为准，本项目不提供免费额度，也不能保证供应商永远免费。

### 单独启动各服务

在仓库根目录执行；Python MCP 自动读取 `.env`。直接运行 Java 时需要先导出对应环境变量。

```bash
.venv/bin/graphics-mcp creative --port 8766
.venv/bin/graphics-mcp webscan --port 8767
java -jar target/SpringAITest-0.0.1-SNAPSHOT.jar --spring.profiles.active=graphics
```

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| Agent / 工作台 | `127.0.0.1:8080` | 任务 API、页面和本地图片 |
| Creative MCP | `127.0.0.1:8766/mcp` | 设计、生图、外观检查 |
| Web-scan MCP | `127.0.0.1:8767/mcp` | 包装已有本地重建 API |
| Web-scan | `127.0.0.1:8787` | COLMAP、Brush、3D viewer 和产物 |

所有服务面向本机单用户使用。不要直接公开到互联网；第一版没有多用户鉴权。页面与 Agent 同源，不需要单独部署前端仓库。

## 模型与重建路径

```text
用户描述 → GLM 设计规格 → 参考图
  ├─ 概念图模式：交付参考图
  └─ 3D 模式：12 张候选视图 → 外观检查 → web-scan 导入 / 冻结
               → COLMAP 估计真实相机 → Brush 本机训练 → Gaussian PLY / Viewer
```

**默认免费路径：** CogView-3-Flash 只接收文字，12 张视图复用同一设计描述，逐张改变请求视角。这些是实验性候选图，不是严格一致的多视角数据。外观检查不能证明几何正确，COLMAP 仍可能失败。不会把提示词中的角度当成相机外参，也不会把一张拼图切成“12 张真实视角”。

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

接口立即返回 `202` 和任务 ID。同一 `Idempotency-Key` 与相同请求返回同一个任务；变更描述请使用新 key。`parentJobId` 可记录版本关系，新版本会重新生成参考图。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/graphics/health` | MCP 配置及本地 Brush 状态，不返回密钥 |
| POST | `/api/graphics/jobs` | `prompt`、`mode`（`CONCEPT` / `THREE_D`）、可选 `parentJobId` |
| GET | `/api/graphics/jobs` | 最近 50 个任务 |
| GET | `/api/graphics/jobs/{id}` | 完整状态与结果 |
| GET | `/api/graphics/jobs/{id}/events` | SSE，事件名 `progress`，每次发送完整状态 |
| POST | `/api/graphics/jobs/{id}/cancel` | 停止后续步骤并取消下游任务 |
| GET | `/api/graphics/assets/{setId}/{name}` | 本地生成的 PNG，仅允许受控 ID 与文件名 |

状态包括 `QUEUED / PLANNING / REFERENCE / MULTIVIEW / IMPORTING / RECONSTRUCTING / VALIDATING / READY`，以及 `CONCEPT_READY / NEEDS_REVIEW / FAILED / CANCELLING / CANCELLED`。

取消无法撤回已发送给供应商的生成请求；任务不会在晚到的结果抵达后变成成功。下游停止结果无法确认时保留 `CANCELLING`，恢复服务后可再次取消。重启会查询并取消遗留任务，不自动续训或重发生成请求。

## 内置 MCP 与数据

工具表、调用约束、部署结构和验收清单见 [技术方案](docs/graphics-agent.md)。实现都在本仓库，**没有修改 web-scan 仓库**，它通过现有 HTTP API 被复用。

```text
src/main/java/.../graphics/    Java MCP 客户端、任务编排、API
src/main/resources/static/graphics/  无构建依赖的工作台
mcp/graphics_mcp/             Creative / Web-scan MCP
scripts/                     本地启动与进程管理
.graphics/                   本地工作数据（忽略提交）
  workflows/                 原子写入的 Java 任务记录
  assets/                    生成图片及清单
  mcp.sqlite3                生图任务、幂等与 web-scan 映射
```

web-scan 自己的数据库、训练缓存和 Gaussian 模型仍保存在它的数据目录。通过 `GRAPHICS_DATA_DIR` 修改 Agent 数据目录时，需要让 Java 与两个 MCP 使用相同路径。不要同时启动多个 Agent 或同种 MCP 操作同一数据目录。

## 验证

```bash
mvn verify
.venv/bin/python -m pytest mcp/tests -q
node --check src/main/resources/static/graphics/studio.js
```

自动测试不调用外部模型，不消耗 token，也不启动 GPU 训练。覆盖任务状态、取消竞态、重启处理、丢失响应、幂等、外观检查失败、SfM 失败、产物缺失、HTTP 导入契约、MCP SDK 工具声明与 API 输入校验。

真实验收仍需配置个人 Key，分别尝试花瓶、香插、摆件，检查 12 张图、SfM 注册数量、PLY 和实际旋转观感。当前版本不宣称已完成真实生图到 3D 的质量验收。

旧 RAG 测试需要 Redis、模型服务和 PDF，默认不运行。显式设置 `RUN_RAG_INTEGRATION=true` 与 `RAG_TEST_PDF` 后可运行。原聊天模块说明保存在 [历史文档](docs/legacy-chat.md)；本次升级到 Spring Boot **3.5.16** / Spring AI **1.1.8**，Java 目标版本仍为 **17**。

原仓库明文 API 配置已替换为环境变量。若原密钥曾有效，应在供应商后台撤销或轮换；删除当前文件中的值不会清除 Git 历史。

运行原聊天模块时不传 `graphics` profile，并配置 `CHAT_API_KEY`、`CHAT_BASE_URL`、`CHAT_MODEL`、`MYSQL_URL`、`MYSQL_USER`、`MYSQL_PASSWORD`、`REDIS_HOST`、`REDIS_PORT`。旧模块仍依赖相应数据库和模型服务。
