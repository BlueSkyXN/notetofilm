---
title: NoteToFilm
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8765
base_path: /space
suggested_hardware: cpu-basic
short_description: Turn scripts into narrated picture videos
---

# NoteToFilm · 出口成片

> Turn scripts into narrated picture videos. 文稿变视频，图音先过审。

自托管的静态图＋旁白视频工具：**提交文稿 → 确认完整分镜计划 → 生成图音 → 逐镜复核当前版本 → 合成 MP4**。

FastAPI、SQLite、本地文件与 FFmpeg；单容器、单应用进程，无需本地 GPU。LLM、生图和配音会访问外部服务，**自托管不等于内容不出网**。

## 当前架构与保障

- 分镜计划及所有分镜先持久化，图音生成失败不会把已完成前缀误认为完整任务。
- 提交后先停在「待确认分镜」。只有人工确认镜数与文稿内容后，才生成图片和配音。
- 任务操作在 SQLite `BEGIN IMMEDIATE` 事务内认领；去重、状态检查及容量判断不可被并发请求穿过。
- 一个任务同时只执行一个修改操作；生成、重做、恢复、合成共用容量上限，渲染另有较小上限。
- 有界线程池执行；运行状态保存在数据库。停机停止新请求，取消运行中的外部请求与渲染进程组；重启不自动重发外部请求，而是标为中断，人工恢复。
- 同一数据目录持有进程锁，第二个应用进程启动失败。**不支持多 worker 或多副本共享数据目录。**
- 图音素材按操作保存独立版本；临时生成、媒体校验成功后再发布。确认绑定版本，旧页面不能确认新素材。
- 占位图不能通过图文复核、不能合成；配音复核不受占位图影响。处理中禁止确认、重做和合成冲突操作。
- 合成冻结素材版本及 SHA-256。每次合成使用独立目录，经过视频校验后写入完成标记，再发布下载地址。
- 恢复会核对完整计划、素材状态和完成标记。缺失或结果未知的付费图片请求必须再次确认；已有成功素材不重复生成。
- 没有自动重试付费请求。上游超时可能已计费，人工重试仍可能产生第二次费用。每任务生图请求数有硬上限，失败和未知请求也计入，页面显示已发起次数（不是实际账单）。
- 分镜确认前可增删并编辑旁白；计划更新后旧页面不能触发生成。闲置任务可归档解除文稿去重，不删除文件。
- 聊天与生图可分别配置网关；外部 HTTP 在隔离子进程内执行，总超时会终止进程，不仅依赖 socket 空闲超时。图片下载拒绝重定向和私网地址。
- 主机名白名单、跨站写入限制、登录尝试上限和 CSP；磁盘空间不足时拒绝新操作。

普通访客无需登录，任务按签名 HttpOnly Cookie 隔离；只有管理员登录后可编辑配置和查看全部任务。没有账号注册、跨设备恢复或用户费用结算，**仍不是完整的公网多租户平台**。全局额度只是有限防护，公开服务仍应配合上游费用预算、反向代理限流和滥用监控。

## 本地启动

需要 Python 3.11、ffmpeg/ffprobe（含 libx264）。目前进程锁和子进程管理支持 Linux/macOS；Windows 请使用 Linux 容器或 WSL。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 在终端环境中设置 ADMIN_TOKEN（兼容旧 ACCESS_TOKEN）；不要把真实令牌提交到 Git。
.venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8765 --no-proxy-headers
```

打开 http://127.0.0.1:8765 即可使用。访客任务绑定当前浏览器 Cookie，30 天到期、清除 Cookie 或更换设备后无法找回；管理员仍可查看。`/admin` 是静态配置面板，浏览器调用管理员 API 时若未登录会跳转到 `/login`。管理员使用 POST 登录与 12 小时签名 HttpOnly Cookie；API 管理员可用 `X-Access-Token`，不接受 URL token。旧版 `ntf_session` 不升级为管理员会话，需重新登录。

若只在本机试验，可显式设置 `ALLOW_UNAUTHENTICATED_LOCAL=1` 并不配令牌。程序仍会检查请求来源为回环地址；不要在这种模式前面加远程反向代理。无管理员令牌且未显式允许本机模式时，启动失败。本机匿名演示只开放访客功能，不能绕过管理员登录进入设置。

未配置模型网关时，分镜按**空行**分段，图片生成纯色占位，配音仍访问 edge-tts。占位模式不能导出正式成片。自动化测试使用本地夹具，不调用 edge-tts。

## Docker

复制 `.env.example` 为未纳入 Git 的 `.env`，生成并填入随机 `ADMIN_TOKEN`（兼容 `ACCESS_TOKEN`），配置需要的网关参数。示例文件不包含真实令牌。仅 Docker Compose 自动读取 `.env`；本地 uvicorn 使用终端导出的环境变量。

```bash
docker compose config --quiet
docker compose up -d --build
```

- `ADMIN_TOKEN` 与 `ACCESS_TOKEN` 均缺少或留空时，Compose 会报错。
- 默认只发布到 `127.0.0.1:8765`。远程访问通过受控 HTTPS 反向代理或 SSH 隧道；HTTPS 部署时设置 `COOKIE_SECURE=1`，把实际访问域名加入 `ALLOWED_HOSTS`，反向代理保留原始 Host。
- 数据持久化到 `./data`，提供 `/healthz` 健康检查。应用只能单进程运行。容器以 UID/GID `1000:1000` 运行，Linux 主机需预先创建目录并赋予对应用户写权限，例如 `mkdir -p data && sudo chown 1000:1000 data`；已有数据先备份、确认归属后再调整权限。
- 镜像构建会运行离线回归及真实 FFmpeg 测试，失败则停止构建。模型请求和 edge-tts 联通性仍需在实际运行环境另行验证。

## Hugging Face Docker Space

使用同一 Dockerfile，CPU Basic 即可，无需 GPU。创建时选择 `Docker`、`cpu-basic` 与 `Protected`；Protected 隐藏源码，但应用地址依然可能被任何人访问，**不能保护公开业务接口；仅管理员设置需要令牌登录**。

- 在 **Settings → Variables and secrets → Secrets** 设置 `ADMIN_TOKEN`（兼容现有 `ACCESS_TOKEN`，建议随机 32 字节以上）。首次启动可从环境迁移模型配置：共用网关时设置 `OPENAI_BASE` + `OPENAI_API_KEY`；分开时设置 `LLM_BASE` + `LLM_API_KEY`、`IMAGE_BASE` + `IMAGE_API_KEY`。不要使用示例 key，不在 README、Variables 或提交记录保存真实凭据。
- 非敏感设置可用 Variables：`TTS_VOICE=zh-CN-YunxiNeural`、`MAX_ACTIVE_TASKS=2`、`MAX_RENDER_TASKS=1`，以及网关实际支持的 `LLM_MODEL`、`IMAGE_MODEL`、`IMAGE_SIZE`。
- HF 注入 `SPACE_HOST` 后，程序自动加入该确切域名并强制 Secure Cookie。端口为 `8765`，README 的 `app_port` 已匹配；不要开启多副本或多 worker。
- Space 内嵌页只显示入口，点击「新窗口打开」直接使用工作台，管理员通过 `/admin` 登录。业务页仍禁止嵌入，不依赖第三方 Cookie，也不放宽跨站付费操作限制。
- **本配置未购买持久化存储**。SQLite、图片、音频与视频位于容器临时盘，重启、休眠或重建可能丢失。成片及时下载；需要长期保存时使用自托管持久化磁盘，不直接把 SQLite/进程锁放到未经验证的对象存储挂载上。
- edge-tts 无需单独 API key，但旁白会发送到微软服务；HF 出网是否可用需实际验证。未配置模型网关时只能空行分镜和占位图，不能导出正式成片。
- HF Secrets 写入后不可回读原值。管理员可在 Settings 替换 `ADMIN_TOKEN` / `ACCESS_TOKEN`；修改会使现有管理员及访客 Cookie 失效，并可能触发 Space 重启。旧访客任务仍保留在管理员列表（前提是数据盘未丢失）。

## 配置

首次启动把环境中的模型参数迁移为 `data/config.toml`（0600）；已有文件优先，后续模型环境参数不覆盖面板修改。API key 永远由环境 / Secrets 解析，允许仅配置 key，再由面板配置地址。部署安全设置和额度仍由环境控制。

| 参数 | 默认值 / 用途 |
|---|---|
| `ADMIN_TOKEN` / `ACCESS_TOKEN` | 管理员令牌；新名称优先，兼容原 HF Secret |
| `ALLOW_UNAUTHENTICATED_LOCAL` | `0`；仅本机演示可显式设 `1`，Compose 不开启此选项 |
| `COOKIE_SECURE` | `0`；HTTPS 部署设 `1` |
| `ALLOWED_HOSTS` | `127.0.0.1,localhost,::1`；远程域名需显式加入，不允许通配符 |
| `LLM_BASE` / `LLM_API_KEY` | 可选独立聊天网关；首次迁移时与地址配套，留空回退至 OPENAI 配置 |
| `IMAGE_BASE` / `IMAGE_API_KEY` | 可选独立生图网关；首次迁移时与地址配套，留空回退至 OPENAI 配置 |
| `OPENAI_BASE` | 空；带 API 前缀的 HTTPS 网关地址，仅默认或 443 端口，例如 `https://gateway.example/v1` |
| `OPENAI_API_KEY` | 空；只用于密钥引用，不写入 TOML |
| `LLM_MODEL` | `origin-deepseek-v4-1-flash`；须按网关能力设置 |
| `IMAGE_MODEL` | `gpt-image-2.5-sunburst`；须按网关能力设置 |
| `IMAGE_SIZE` | `1024x768`；须与图片接口契约匹配 |
| `IMAGE_TIMEOUT` | `300` 秒；单次图片接口 HTTP 请求总时限，图片下载另限 60 秒 |
| `STYLE_PRESET` | 简洁蓝白扁平插画，不包含文字 |
| `TTS_VOICE` | `zh-CN-YunxiNeural` |
| `FEISHU_WEBHOOK` | 空；可选，只发送任务编号和状态 |
| `VIDEO_DATA_DIR` | `data/`；Compose 固定使用挂载的数据目录 |
| `MAX_ACTIVE_TASKS` | `3`；包括规划、生成、重做、恢复、合成的在途操作总量 |
| `MAX_RENDER_TASKS` | `1`；不能大于总容量 |
| `MAX_SCRIPT_CHARS` | `20000`；文稿和分镜总长度上限；实际请求体字节上限为该值 × 6 + 196608 |
| `REQUEST_BODY_TIMEOUT` | `30` 秒；接收完整请求体的总期限，不随分块重置 |
| `MAX_SCENES` | `30`；超限报错，不再截断 |
| `MAX_NARRATION_CHARS` | `2000`；单镜旁白长度上限 |
| `RENDER_TIMEOUT` | `1800` 秒；超时终止整个渲染进程组 |
| `MAX_IMAGE_REQUESTS_PER_TASK` | `60`；初次、重做和失败生图请求合计上限，不代表金额预算 |
| `MIN_FREE_DISK_MB` | `256`；低于此值拒绝新操作，仍需管理员监测长任务过程中的磁盘 |
| `MAX_AUDIO_SECONDS` | `180`；单镜音频媒体校验上限 |
| `MAX_VIDEO_SECONDS` | `900`；合成前旁白总时长上限 |

兼容范围是 **Chat Completions＋Images generations** 这两种具体请求契约，不是所有自称 OpenAI-compatible 的服务。聊天与生图默认共用 OPENAI base/key，也可分别配置独立网关；Images 必须支持配置的模型、尺寸及所选 `quality`（默认 low），并返回 base64 或 HTTPS 图片地址。适配层在 `app/providers.py`。管理员 TOML 编辑器支持分别配置聊天与生图网关、模型、尺寸、质量、超时和音色。

## API 优先架构

全部业务接口统一为 `/api/v1`，前端只请求 API，不依赖服务端模板注入。任务、图音审批、合成、文件下载、访客会话、管理员登录和配置导出均可脱离页面调用。异步操作返回 202 与状态查询地址，错误统一带请求编号，响应 schema 不暴露内部配置及文件路径。

接口列表与接入说明见 [API.md](API.md)；运行后的 `/api-docs` 是只读文档页，`/api/v1/openapi.json` 可生成客户端。前后端可以同容器提供，也可由同源反向代理分别托管，不引入微服务或放宽跨站写入。旧 `/api/*` 与 `/files/*` 接口已移除，部署需同时更新前端。

## 工作台与成片

工作台并列展示文稿输入与任务列表，任务摘要取首镜旁白或原稿前 60 字。任务页按分镜确认、图音复核、处理中、失败/中断及完成状态显示当前动作；分镜修改未保存时禁止生成并提示离开风险。完成后可直接播放视频，或使用独立下载接口保存 MP4；原媒体接口继续用于内联预览。

所有页面共用明暗主题。优先使用已保存偏好，否则跟随系统；主题开关可随时切换。手机端按内容顺序排列，不提供与实际任务无关的占位预览。

## 管理员配置面板

`/admin` 提供 TOML 编辑、校验、保存并应用、恢复上一版与导出。只有管理员可以读取或写入配置；访客响应不包含网关地址、密钥引用或完整配置快照。

- `[llm]`：`base_url`、`model`、`api_key_env`。
- `[image]`：`base_url`、`model`、`api_key_env`、`size`、`quality`、`timeout_seconds`、`style`。
- `[tts]`：`provider="edge-tts"`、`voice`。
- `api_key_env` 只允许 `OPENAI_API_KEY`、`LLM_API_KEY`、`IMAGE_API_KEY`；不能借此读取管理员令牌或任意环境变量。真实 key 不下发浏览器，只显示已配置状态。
- 网关使用无凭据、无查询参数的 HTTPS 443 地址。更换网关或密钥引用必须再次确认授权绑定，避免无意把旧密钥发往新服务。
- 保存前做语法、字段、类型、限值和密钥检查；使用版本冲突检查、原子替换、`config.previous.toml` 备份。非法配置不替换生效版本，磁盘被外部修改时拒绝覆盖。
- 保存无需重启，只影响新任务；每个任务持久保存无密钥的配置快照。旧任务继续使用旧模型、地址和音色，不会静默切到新网关。密钥不冻结；移除旧 key 后，历史详情、已完成视频下载、本地合成和已有成片恢复仍可用，只有确实需要调用对应供应商时才要求恢复兼容密钥。已存在的合法 TOML 可以在模型密钥暂时缺失时启动；首次配置迁移及保存新配置仍要求所引用密钥存在。
- 校验、保存、恢复不触发模型调用；不把语法校验宣称为真实供应商联通性验证。
- Docker 挂载 `data` 即保留配置。HF 当前临时盘上的配置会丢失，面板明确提示并支持导出；恢复时可粘贴导出文本，或自托管启动前放入 `data/config.toml`。不自动写 HF 仓库，不新增 HF 写权限凭据。

## 公开使用额度

SQLite 事务内按 UTC 日计数；失败、未知请求也计入。不是金额预算，也不是防机器人系统。管理员与所有访客共享全局额度，归档和换 Cookie 不解除全局额度。

| 环境变量 | 默认上限 / UTC 日 |
|---|---|
| `MAX_DAILY_TASKS` | 全服务 20 个新任务 |
| `MAX_VISITOR_DAILY_TASKS` | 每个访客 5 个新任务 |
| `MAX_DAILY_LLM_REQUESTS` | 全服务 40 次分镜模型请求 |
| `MAX_DAILY_IMAGE_REQUESTS` | 全服务 60 次生图请求 |
| `MAX_DAILY_AUDIO_REQUESTS` | 全服务 120 次配音请求 |
| `MAX_DAILY_RENDER_REQUESTS` | 全服务 30 次合成操作 |

额度在实际请求前认领，中途耗尽会停止任务并保留已完成素材。任务的单独上限、总并发和磁盘限制仍然生效。历史无所有者任务迁移为管理员所有，不公开给访客。

## 代码结构

```text
app/app.py          服务生命周期、安全中间件与静态前端入口
app/api.py          API v1 路由、鉴权依赖与文件出口
app/schemas.py      请求/响应契约与 OpenAPI schema
app/config.py       部署环境与初始配置
app/model_config.py TOML 校验、原子写入、热应用与任务快照
app/auth.py         匿名访客 Cookie 与任务身份
app/db.py           数据迁移、事务、单进程锁、启动恢复
app/tasks.py        完整计划、原子认领、任务编排、版本复核
app/providers.py    LLM、图片、TTS、通知接口
app/render.py       媒体验证、冻结清单、合成尝试及完成核验
app/templates/     页面
app/static/        CSS 与原生 JavaScript
tests/             离线回归、HTTP 与 FFmpeg 测试
vendor/compose_video.py  独立 manifest 渲染引擎
```

没有 Redis、Celery、Dify、React 或额外审批服务。

## 数据、恢复与升级

`tasks.db` 保存完整计划及操作状态；`data/{id}/assets/{operation}/` 保存版本化图音；`renders/{operation}/` 保存冻结清单、分镜 manifest、视频、构建报告及 `complete.json` 完成标记。文件出口只提供当前素材和已发布成片，不开放数据库、过程文件及失败半成品。

恢复规则：

- 完整计划存在：保留成功素材，仅处理缺失/失败项；未知生图必须人工确认。
- 合成已完成但状态尚未落库：核对完成标记、文件哈希、当前素材版本及媒体有效性后恢复完成。
- 半成品没有有效完成标记：不视为成片；可在新的 attempt 目录重新合成，旧目录不覆盖。
- 旧版库没有完整计划：非完成任务标为中断，恢复前要求确认重新规划，**不把旧镜头前缀当完整任务**。旧文件不删除。旧版本已标记完成的成片会重新检查构建报告、媒体流与时长，通过后继续提供下载；不通过则标为中断并保留原文件，不能只凭文件存在认定完成。

生图重做现在必须携带 `expected_revision` 与 `idempotency_key`，同键同参数重放原受理结果，不再调用供应商；同键不同参数或过期版本返回冲突。台账跨完成/失败/重启保留，不提供通用请求幂等保证。SQLite schema 升为 v4，新增幂等台账；不会删除已有任务或媒体。旧客户端必须与后端同步升级。

归档只改变任务状态，不释放磁盘。磁盘不足时由管理员先备份再清理或迁移存储；本程序不自动删除历史素材。

**升级前停服务并备份整个 data 目录。** 数据库迁移后不建议直接降级；回退应使用完整的迁移前备份。重启恢复不会自动发起付费调用。当前不自动清理历史素材和失败尝试，管理员需要监控磁盘并安排停机备份与人工归档。

## 验证

在仓库根目录运行：

```bash
.venv/bin/python -B scripts/check_tests.py
```

测试包含实际请求体限额/读取超时、并发去重/容量、持久生图重做幂等、过期版本确认、部分失败恢复、密钥迁移后的历史读取、启动中断、旧库迁移、HTTP 鉴权、跨站写入、真实本地 FFmpeg 合成与损坏产物拒绝。模型和 TTS 均使用测试替身；FFmpeg 测试仅在本地生成颜色图与正弦波，不包含外部素材。严格入口要求 FFmpeg/ffprobe 与编码器存在、测试数量大于零且无 skipped；缺少媒体工具不能算通过。普通 unittest discover 仍可用于诊断，但它可能跳过媒体测试。

`.github/workflows/tests.yml` 对 push/PR 执行同一严格回归、前端语法检查与生产镜像构建；Dockerfile 也使用严格入口。工作流不读取生产 Secrets。运行结果以相应提交的 Actions 记录为准，配置存在不等于已运行。

请求体中间件在业务处理前累计实际读取字节，拒绝冲突/重复的消息定界头；限额内的分块 JSON 可以使用。反向代理仍应独立配置请求体上限、读取超时和限流；应用测试不代表所有公网代理链已验收。

## 已知边界

- 模型是否忠实覆盖原稿仍需用户在分镜确认页核对；数量和长度校验无法证明语义完整。
- 支持生成前编辑分镜旁白；生成后的旁白编辑、时间线同步预览和成片后二次编辑尚未提供。可归档原任务后重新提交，旧文件保留。
- 2fps 低成本合成存在帧边界舍入；真实播放效果和换镜节奏需要业务验收。
- 访客 Cookie 不是实名身份；清 Cookie 可绕过单访客额度，但不能绕过持久数据库中的全局额度。额度耗尽可造成他人无法使用；HF 临时盘丢失会重置计数，上游还必须设置费用预算。
- API key 仅从环境读取；网关错误不把原始响应写给用户，服务日志只记录任务/操作编号和错误类型。
- edge-tts 没有企业 SLA，文本会发往微软端点；网关配置不代表模型处理一定在内网。
- LICENSE 暂未添加，默认保留所有权利，沿用当前发布决定。
