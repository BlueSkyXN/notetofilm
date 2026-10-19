---
name: notetofilm-workflow
description: "从材料制作完整视频，或修改、继续本地视频项目。用于需要安排材料、脚本分镜、图像、视频、配音和合成的跨阶段任务；读取项目现状并执行到用户要求的结果。"
---

# 本地视频工作流

## 开始

1. 先读 [本机入口](references/runtime.md) 和 [命令](references/commands.md)。确定本次目标、项目位置、结束阶段及指定镜头。
2. 没有项目时执行 `ntf init PROJECT --title TITLE`。已有项目先执行 `ntf status PROJECT`，读取正式文件；不要从旧对话重建已经存在的分镜。
3. 需要写入内容时读 [项目格式](references/project-format.md)。根据当前输入按 [阶段路由](references/routing.md) 读取相应已安装 Skill。通过客户端提供的位置或安装后本机入口的清单定位，不拼接同胞目录。

## 推进

4. 由当前 Agent 整理材料和编写正式分镜。Skill 是 Agent 的工作方法，不是 Python 可执行任务。
5. 正式分镜保存后执行 `ntf plan PROJECT`。特殊图音顺序或明确依赖使用 `ntf plan PROJECT --file FILE`。读 [依赖与修改](references/dependencies.md)，给需要用户语义判断的内容留在内容步骤中。
6. 执行 `ntf ready PROJECT`。读取所需媒体 Skill 的方法，执行返回的动作 ID。公司通道调用传实际配置文件；Edge TTS 走本地音频适配。
7. 使用 `ntf run PROJECT --action ID --config FILE --wait-seconds 20` 执行或查询。只要旧动作仍可查询就继续它，不用新提交模拟轮询。
8. 每次取得实际文件后登记结果，再查看状态和可执行动作。图片和配音可按工具能力并行；依赖参考图的视频等待图像完成。最终合成等待实际媒体。
9. 需要改变正式旁白、事实或屏幕文字时回到导演方法，保存主记录并重新 `ntf plan`；不要只改生成提示而让正文保持旧值。

## 完成

按用户要求在材料、分镜、某项素材或 MP4 完成后结束。给出实际文件和项目位置，说明仍未完成的具体项。只有模型任务 ID 或提示词时，描述为已提交或已设计，不描述为已出片。

当前协议尚未配置时，继续完成不依赖该通道的内容、已知媒体和计划，指出缺少的具体连接信息。不要擅自切到官方接口、其他模型或另一家服务。
