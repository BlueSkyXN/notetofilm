# NoteToFilm-local 0.1.0：先从这里开始

这份包包含七个 Skill、完整 Python 运行代码、六个模型 ID 配置、公司协议适配、安装器、测试和一个可播放的离线样片。

## 1. 不安装也能先看结果

打开 `examples/demo/output/video.mp4`。这是 4.6 秒的混合媒体技术样片，覆盖图片、视频、裁剪、末帧延长、独立音轨、原声、混音、静音与字幕。它使用程序生成的测试图和测试音，供验证合成流程，不代表 AI 内容创作效果。

## 2. 先在本地重跑合成

需要 Python 3.11+、FFmpeg 和 ffprobe。在解压后的 `notetofilm-local` 目录中执行：

```bash
PYTHONPATH=runtime python3 -m notetofilm_local compose examples/demo --manifest examples/demo/compose.json
PYTHONPATH=runtime python3 -m notetofilm_local status examples/demo
```

新成片写入示例项目的 `renders/`，自带样片保留。无需公司密钥或 Edge TTS 网络连接。

要从零生成一套测试素材并合成，可运行：

```bash
python3 examples/create_media_fixture.py --output /tmp/notetofilm-demo-new --render
```

## 3. 安装七个 Skill 和独立运行环境

把 `--skills-dir` 换成你选定客户端实际读取的技能目录：

```bash
python3 scripts/install.py \
  --skills-dir /path/to/client/skills \
  --runtime-dir /path/to/notetofilm-runtime \
  --with-audio
```

安装器输出真正的 `ntf_command`，并写入每个 Skill 的运行位置说明。省略 `--with-audio` 时，安装本身无需下载 Python 依赖；以后可在该独立 Python 环境安装 `edge-tts==7.2.8`。

对 Agent 说：

> 使用 notetofilm-workflow，把这些材料做成视频。先整理来源和正式分镜，再按需要生成图片、视频和旁白，最后输出 MP4。已有素材优先复用。

也可以只调用 image、video、audio 或 compose 技能，不必跑完整制作流程。

## 4. 接通公司的图像与视频 API

先阅读 `docs/company-protocols.md`。`config/company.example.json` 已包含全部六个原样模型 ID；`config/protocols.example.json` 展示显式协议配置。

需要填写公司 Base URL、认证方式及环境变量名、该接口使用的协议、实际支持模式和参数。程序不会根据 `veo`、`seedance` 或 `gpt-image` 字样猜接口，也不会自动转往原厂。

公司只改地址、认证头和请求路径时可以配置。公司重写请求体、上传流程、状态格式或响应结构时，需要按说明扩展适配器。未提供这些公司资料前，不能承诺六个模型已经实际生成成功。

## 5. 配音和续做

```bash
ntf audio /path/to/project --text "这是一段配音。" --voice zh-CN-XiaoxiaoNeural
ntf run /path/to/project --config /path/to/company.json --wait-seconds 20
ntf status /path/to/project
```

如需代理，可在本机配置 `NTF_EDGE_PROXY`；代理值不会写进项目音频元数据。Edge TTS 需要联网，本包没有预置语音引擎或公司密钥。

改正式分镜后再运行 `ntf plan`。对已完成动作明确重做用 `ntf run --action ID --force`；失败或提交结果不明时先看状态，再决定是否 `--retry`。已取得远端任务 ID 的工作会继续查询，不自动再次提交。

完整字段、目录、技能分工见 `README.md`；本次实测范围见 `docs/RELEASE_STATUS.md`。
