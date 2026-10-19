# 视频模式与公司通道

目录包含 `veo-3.1-generate-preview`、`doubao-seedance-2-5`、`doubao-seedance-2-0-260128`。它们是公司通道标识，不保证采用供应商官方协议。

每条实际通道需明确：文生或图生等模式，参考图作用、数量与传输方式，片长与画幅，是否带声音，提交、查询和下载协议。

文生视频以动作与运镜说明为主；图生视频使用实际采用的输入图；首尾帧分别表示时间位置。多参考图描述外观时，不将其当成必须依次出现的帧。

先用 routes 阅读实际配置；protocol 或公司路径未确定时保留计划并说明缺项，不猜默认请求。已明确要求的通道未接通，不自动换成另一条。

单项文生视频请求采用扁平 JSON：

```json
{"kind":"video","route":"veo-3.1-generate-preview","mode":"text_to_video","prompt":"镜头缓慢靠近正在整理资料的工作台，动作自然。","selection_slot":"s02.video"}
```

图生视频使用 `mode=image_to_video` 与 `start_frame`；多参考视频使用 `reference_to_video` 与有序 `references`；首尾帧使用 `first_last_to_video` 及独立 `start_frame`、`end_frame`。是否可调用取决于该公司通道声明，示例不是已确认的在线请求。实际片长与其他模型参数填入 `parameters`。

原片音轨由合成策略决定保留、替换或混合。片段存在人声时，不能把混合原声宣称为可单独使用的环境音。
