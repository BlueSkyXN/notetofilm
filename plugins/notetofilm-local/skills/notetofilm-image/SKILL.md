---
name: notetofilm-image
description: "按分镜或单独画面要求制作图片，支持配置中的普通生图和多参考图生图。用于生成、替换、复用镜头图片或给视频制作参考图；调用实际通道并登记取得的媒体。"
---

# 图像制作

1. 读 [本机入口](references/runtime.md)、[命令](references/commands.md) 和 [项目格式](references/project-format.md)。单独生图也建立最小项目，不要求先编写无关旁白。
2. 读取指定镜头或用户明确画面要求。按 [画面与参考](references/image-design.md) 分开主体、构图、准确屏幕内容、风格与参考图作用。
3. 用 `ntf routes --config FILE` 读取实际图像通道。读 [公司通道](references/company-image.md)，只使用已配置协议和已确认模式；模型名称不代表已接通。
4. 原图先 `ntf import`，保留返回 ID。多参考图按实际顺序传对象列表，并说明每张图的作用。普通外观参考与视频首尾帧不是同一种约束。
5. 工作流任务读取 `ntf ready` 中的具体图像动作并执行 `ntf run`。单项生成使用 `ntf generate PROJECT --request FILE --config FILE`，请求结构按 [公司通道](references/company-image.md)。
6. 若用户明确选用当前客户端图像工具，由 Agent 调用并取得实际文件，再用 `ntf import` 登记。不要把公司通道失败自动改成客户端工具调用。
7. 返回实际图片、尺寸、素材 ID 与本次使用的参考。采用某张图时 `ntf select PROJECT SLOT ASSET_ID`；保留其他候选。

需要更改数字、屏幕标题或原意时先更新正式分镜。不能可靠生成的文字和关系，用实际可用排版能力制作整张 PNG；只改变背景风格不需要重写旁白。
