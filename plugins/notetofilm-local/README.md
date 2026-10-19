# NoteToFilm-local

NoteToFilm-local 是一个本地视频制作插件：**七个 Skill 负责理解与组织制作，共用 Python 代码负责项目、媒体调用、结果恢复和 FFmpeg 合成。**项目与媒体保存在本机；图像、视频和 Edge TTS 可以使用联网服务。

当前采用本地技能目录安装方式，并提供 Agent Plugins 1.0 的 portable `plugin.json`。目录安装成功不等于所有客户端都支持相同的原生插件安装流程；按选定客户端实际支持的技能目录加载即可。

## 七个 Skill

| Skill | 工作 |
|---|---|
| `notetofilm-workflow` | 根据现有项目安排完整制作、局部修改和中断后继续 |
| `notetofilm-prepare` | 阅读原材料，保存来源、原图与制作说明 |
| `notetofilm-direct` | 写唯一正式分镜、旁白和屏幕信息 |
| `notetofilm-image` | 普通生图、多参考图生图与图片复用 |
| `notetofilm-video` | 视频片段提交、查询、下载与参考模式处理 |
| `notetofilm-audio` | Edge TTS 配音、实际时间信息与字幕 |
| `notetofilm-compose` | 图片、视频、旁白与字幕的混合成片 |

同一个 Agent 可以依次读取这些 Skill，无需七个 Agent 同时运行。Skill 不是 Python 可以执行的程序；实际调用发生在客户端工具和 `ntf` 命令中。

## 安装

需要 Python 3.11 或以上。合成需要可以执行的 `ffmpeg` 与 `ffprobe`。Edge TTS 需要额外 Python 依赖和在线服务连接。

在本插件目录执行，填入两个明确、互不嵌套的安装位置：

```bash
python3 scripts/install.py --skills-dir /path/to/client/skills --runtime-dir /path/to/notetofilm-runtime --with-audio
```

- `--skills-dir`：客户端实际读取的技能目录，已有其他技能保留。
- `--runtime-dir`：本插件独立运行环境，内含完整代码和 venv。
- `--with-audio`：在这个 venv 中安装 Edge TTS。省略时安装过程本身不下载 Python 依赖，材料、分镜与已有媒体操作仍可使用。
- `--upgrade`：更新同一路径中由本插件安装器建立的旧版本。同名但不属于本插件的目录不会被覆盖。

安装后使用 JSON 返回的 `ntf_command`；以下使用类 Unix 路径示例：

```bash
/path/to/notetofilm-runtime/bin/ntf install-info
```

各 Skill 的 `references/runtime.md` 包含实际命令、Python 环境及七个技能的位置。代码从安装环境读取，不依赖原源码目录，也不需要跨 Skill 相对路径。客户端重新加载方式见其实际说明；本项目的[目录安装说明](client-packages/skills-directory/README.md)说明当前包装范围。

也可以在自己管理的独立 Python 环境使用 `pip install .` 或 `pip install '.[audio]'` 安装运行包，然后按实际客户端方式加载 `skills/`。这种方式需要自行提供准确的运行命令位置。

## 公司图像与视频配置

模型目录包含用户指定的六个 ID：

| 类型 | ID |
|---|---|
| 图像 | `gpt-image-2.5-sunburst` |
| 图像 | `gpt-image-2` |
| 图像 | `gpt-image-2.5-flare` |
| 视频 | `veo-3.1-generate-preview` |
| 视频 | `doubao-seedance-2-5` |
| 视频 | `doubao-seedance-2-0-260128` |

这些是公司模型标识，**不代表默认已经接通，也不代表自动采用厂商官方协议**。公司地址、认证引用、提交和查询路径、返回结构、图像参考及视频模式必须由实际配置确定。

```bash
ntf config-init /path/to/company-config.json
ntf routes --config /path/to/company-config.json
```

生成的目录保留未确认状态。根据公司接口资料填写配置后，查看实际能力再执行。普通生图、多参考图及各视频模式分别按通道声明使用；不会静默丢掉参考图或自动改走另一个厂商。

运行参数中的凭据通过配置引用的环境变量取得。项目保存使用的模型、协议和输入信息；不要把凭据写进 Skill、主分镜或待发布配置。

当前接口实现与测试中的模拟请求和响应用于验证协议处理，不等于公司实际联通结果。取得公司文档或请求响应示例后，按实际协议配置相应通道。

## 一次完整制作

可以直接对 Agent 说：

> 使用 notetofilm-workflow，把这些材料做成一条横版讲解视频，包含需要的图片、视频片段和旁白，最后输出完整 MP4。

Agent 依次完成材料、分镜与所需媒体；中间文件无需用户手动转交。

运行步骤可对应到以下命令，其中 `FILE` 是 Agent 已写好的真实文件：

```bash
ntf init /path/to/project --title "流程讲解"
ntf storyboard /path/to/project --file /path/to/project/work/storyboard-draft.json
ntf plan /path/to/project
ntf ready /path/to/project
ntf run /path/to/project --config /path/to/company-config.json --wait-seconds 20
ntf status /path/to/project
```

具体动作可用 `--action ID` 单独执行，ID 从计划或 `ready` 读取。未取得结果时再次 `run` 继续已有动作；`--retry` 用于明确的新尝试，`--force --action ID` 用于明确重做已完成动作。提交结果不明时不自动重复提交。

### 只做一部分

- “使用 notetofilm-prepare，先整理这几份材料和原图。”
- “使用 notetofilm-direct，保留旁白原文，只设计画面和分镜。”
- “使用 notetofilm-image，用这几张参考图生成第二镜画面。”
- “使用 notetofilm-video，继续取得上次已提交的视频片段。”
- “使用 notetofilm-audio，只重做第三镜配音。”
- “使用 notetofilm-compose，按现有图音顺序重新合成。”

已有图音可以直接导入和合成，不要求虚构内容分镜：

```bash
ntf init /path/to/assembly --title "现有素材合成"
ntf import /path/to/assembly /path/to/image.png --select s01.image
ntf import /path/to/assembly /path/to/audio.mp3 --select s01.audio
ntf compose /path/to/assembly --manifest /path/to/assembly/manifest.json
```

清单使用项目内素材路径和明确顺序；[合成清单示例](skills/notetofilm-compose/references/manifest.md)说明字段。`--plan-only` 只计算安排，省略才执行编码。

## 文件与修改

- `project.json`：制作说明和项目设置。
- `sources/`、`materials.md`：实际材料、来源和整理结果。
- `storyboard.json`：唯一正式分镜；`views/` 为阅读版。
- `workflow.json`：当前动作、输入引用与直接依赖。
- `state.json`：由运行代码登记结果、素材及采用关系。
- `assets/`：已经取得的图片、音频与视频。
- `renders/`：各次实际输出与时间安排。

材料与导演由当前 Agent 编写正式内容；运行代码不替代内容创作。改分镜后再次 `ntf plan`，按相关输入决定下一步。配音变化先更新时间，不自动让所有视频重新生成。新候选和新成片保留旧版本，未改变的媒体可以复用。

原始材料使用 `source_id`，媒体使用 `asset_id`。需要制作的原图先导入媒体登记，再用素材 ID 或采用关系引用。首尾帧和多张外观参考分开记录。

## 源码与打包

源码中的 `skills/`、`shared/`、`runtime/` 和客户端包装位于同一插件目录；视频项目放在用户选择的工作位置。安装器只更换本插件已标记的安装目录。

在开发环境运行测试：

```bash
PYTHONPATH=runtime python3 -m unittest discover -s tests -v
```

生成包含本插件源码、技能和运行包的私有分发包：

```bash
python3 scripts/package.py --output /tmp/notetofilm-local-0.1.0.zip
```

归档写在源码目录之外，含一个 `notetofilm-local/` 根目录；不包含已安装环境或本机定位文件。发布副本中的技能参考资料自包含。该打包动作不自动安装到用户客户端、上传插件目录或连接公司服务。
