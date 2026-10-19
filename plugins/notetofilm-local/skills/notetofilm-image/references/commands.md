# 本地命令

先读本 Skill 的 `runtime.md`，使用其中安装器生成的实际命令路径。下面的 `ntf` 代表该命令。命令输出 JSON；发生错误时读取 `code`、`message` 和具体信息，不把错误输出当成媒体结果。

| 操作 | 命令 |
|---|---|
| 建项目 | `ntf init PROJECT --title TITLE` |
| 查看安装 | `ntf install-info` |
| 查看实际通道 | `ntf routes --config FILE` |
| 导入媒体 | `ntf import PROJECT FILE --select SLOT` |
| 保存正式分镜 | `ntf storyboard PROJECT --file FILE` |
| 从分镜生成媒体计划 | `ntf plan PROJECT` |
| 使用指定计划 | `ntf plan PROJECT --file FILE` |
| 看当前可执行项 | `ntf ready PROJECT` |
| 看进展与输入变化 | `ntf status PROJECT` |
| 执行或继续具体动作 | `ntf run PROJECT --action ID --config FILE --wait-seconds 20` |
| 明确重做已完成动作 | `ntf run PROJECT --action ID --config FILE --force` |
| 采用指定素材 | `ntf select PROJECT SLOT ASSET_ID` |
| 单独生成媒体 | `ntf generate PROJECT --request FILE --config FILE` |
| 单独配音 | `ntf audio PROJECT --text TEXT --voice zh-CN-XiaoxiaoNeural` |
| 按明确清单合成 | `ntf compose PROJECT --manifest FILE` |
| 只计算合成方案 | `ntf compose PROJECT --manifest FILE --plan-only` |
| 创建待配置样例 | `ntf config-init FILE` |

PROJECT 与文件参数都传具体路径。动作 ID 从计划或 `ready` 结果读取，不依赖“第二项”之类临时位置。无需用户手填 JSON：Agent 根据正式材料写入工作文件再调用。

`run` 的等待时间仅控制当前一次等待，未取得文件时继续查询已有动作，不新建相同任务。提交超时且结果不明时，保留动作与请求信息；只有明确决定重发时才使用 `--retry`，不要把它当成循环轮询参数。

`generate` 的请求是扁平 JSON，包含 `kind`（`image` 或 `video`）、`route`、`mode`、`prompt` 及实际需要的参考和参数；不嵌套 `request`。可指定 `action_id`、`selection_slot` 和 `select_index`。同一 action_id 已存在时继续 `run`，不要再次 `generate`。

需要当前客户端提供的文件读取、生图或视频工具时由 Agent 直接调用。取得实际媒体文件后用 `ntf import` 登记和采用；不要宣称 Python 可以执行任意 `SKILL.md` 或自动访问客户端专有工具。
