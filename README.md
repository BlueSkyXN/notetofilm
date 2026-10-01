# NoteToFilm · 出口成片

> Turn scripts into narrated picture videos. 文稿变视频，图音先过审。

贴入文稿 → AI 分镜 → 逐镜配图＋配音 → 人工双闸复核 → 一键合成 MP4。
适合把已有文字材料（培训稿、宣导稿、知识讲解）快速变成"静态图＋旁白"的叙述视频。

- **自托管**：FastAPI＋SQLite＋FFmpeg，单容器部署，无需 GPU
- **自带网关**：LLM 分镜与生图走任意 OpenAI-compatible 网关；配音用 edge-tts 免费端点
- **低成本渲染**：静态图低帧率合成，纯 CPU 即可
- **人工把关**：图、音双闸复核，占位图不能通过图文闸、不能进入成片

## 工作流

1. 首页贴入文稿（空行分段），提交任务
2. 后台逐镜生成：一张配图＋一段旁白（未配置网关时：分镜按空行、图为纯色占位、配音仍真实合成）
3. 复核页逐镜看图、试听，分别确认"图文通过""配音通过"；不满意可单镜重做（重做图＝付费调用，需确认；重配音＝免费）
4. 全部通过后一键合成，下载 `video.mp4`

## 当前状态与边界

- 已用 OpenAI-compatible 网关完成端到端验证（四镜技术样片）；样片确认标记来自接口测试，不代表人工看图核听验收。
- 鉴权是单一共享令牌（cookie / `?token=` / `X-Access-Token`），无按人身份、无配额；配置 `ACCESS_TOKEN` 后适合内网/VPN 多人试用，不可暴露公网。
- 任务执行靠进程内后台线程，无持久队列；进行中任务数受 `MAX_ACTIVE_TASKS`（默认 3）限制，相同文稿处理中不允许重复提交。多人同时复核同一任务仍有竞态。
- 重做会覆盖同名素材；渲染阶段及完成后禁止修改。尚不是完整版本锁定系统。
- 生图失败或未配置网关时使用占位图并标记 `[占位图·需重做]`；占位图不能通过图文闸、不能进入合成。
- 有逐镜图片/音频控件、任务列表与三级恢复（已成片→标记完成／素材齐→回复核／素材缺→确认后整体重跑），没有时间线同步预览，无按镜编辑旁白。
- 服务重启自动把生成中/渲染中任务复位为"已中断"，可在复核页恢复。
- 无自动重试付费请求。超时后上游是否计费可能未知，请自行控制预算。
- LICENSE 暂未添加，法律上默认保留所有权利；整理后补齐。

## 快速开始（本地）

需要 Python 3.11、PATH 中可用的 ffmpeg / ffprobe。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.app:app --host 127.0.0.1 --port 8765
```

浏览器打开 http://127.0.0.1:8765 。仅运行一个应用进程。

## 环境变量

| 变量 | 用途 / 默认值 |
|---|---|
| `OPENAI_BASE` | OpenAI-compatible 网关地址（如 `https://your-gateway/v1`），与 `OPENAI_API_KEY` 成对配置；留空＝占位图演示模式 |
| `OPENAI_API_KEY` | 网关 key；仅通过环境注入，不写入 Git |
| `LLM_MODEL` | 分镜模型，默认 `deepseek-chat`；按你的网关实际可用模型设置 |
| `IMAGE_MODEL` | 生图模型，默认 `gpt-image-2`，`quality=low` |
| `IMAGE_SIZE` | `1024x768`；当前已知契约另支持 `1024x1024` |
| `IMAGE_TIMEOUT` | 生图请求 socket 超时秒数，默认 300；不是整项任务总截止时间 |
| `STYLE_PRESET` | 全片生图风格提示词 |
| `TTS_VOICE` | edge-tts 音色，默认 `zh-CN-YunxiNeural` |
| `FEISHU_WEBHOOK` | 可选；仅发送任务编号和状态，不包含原稿、素材或原始错误信息 |
| `VIDEO_DATA_DIR` | 数据目录，默认工程 `data/`；测试可用独立目录 |
| `ACCESS_TOKEN` | 共享访问令牌；设置后页面与 API 均需携带，留空＝不鉴权（仅本机开发） |
| `MAX_ACTIVE_TASKS` | 同时处于提交/生成/渲染的任务上限，默认 3；超出提交返回 429 |

分镜上限 30，超长内容可能被截断，请先控制输入规模。LLM 失败时回退按空行分段。

## 文件与合成

`data/tasks.db` 保存任务与确认标记，`data/{id}/` 保存图、音频、分镜清单、MP4 与技术构建记录（`video.build.json`）。默认以 2fps 导出；低帧率的换镜精度和目标播放器兼容性以实际成片验收为准。渲染引擎 `vendor/compose_video.py` 以 manifest（`schema_version=1`）为契约，拒绝覆盖已有成片。

备份应停服务后复制整个 data 目录；运行中不要只拷数据库文件。

## 部署（Docker）

```bash
# 同目录 .env 放 OPENAI_BASE / OPENAI_API_KEY / ACCESS_TOKEN 等（已被 .gitignore 排除）
docker compose up -d --build
```

数据持久化在 `./data`。首次构建请在目标机上验证基础镜像与 ffmpeg 依赖可拉取。

## 数据与服务边界

edge-tts 通过微软在线端点合成，旁白文本会出网；模型网关的路由范围以你配置的网关为准。自托管不等于数据全程不出网，请按自己的合规要求选择网关与部署位置。
