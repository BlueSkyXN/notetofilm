# 公司图像通道

目录包含三个不透明图像模型 ID：`gpt-image-2.5-sunburst`、`gpt-image-2`、`gpt-image-2.5-flare`。这些名称不说明协议、参考上限或已经联通。

配置必须明确公司地址、请求路径、认证引用、请求结构和图片返回方式。普通生图与多参考图编辑的传图格式按真实协议设置；不可猜测官方地址。

用 `ntf config-init FILE` 生成样例，再按公司接口资料填写；用 `ntf routes --config FILE` 阅读当前能力。没有确认的能力不自动宣称支持。

单项请求采用扁平 JSON，不嵌套 `request`：

```json
{"kind":"image","route":"gpt-image-2","mode":"text_to_image","prompt":"浅色背景上的三阶段流程图，保留标题排版区域。","selection_slot":"s01.image"}
```

多参考图使用 `mode=reference_to_image`，增加有序 `references`，例如 `{"selection":"reference.product","position":1,"role":"appearance","instruction":"保留产品外观"}`。先导入并采用真实参考文件。具体模型参数放入 `parameters`，仅填写实际通道已声明支持的字段。工作流优先使用 `ntf plan` 已生成的动作，不另建同一生成请求。

多个参考图不受支持时不要静默只传首图，也不默认拼成一张图声称多参考图已经完成。返回需要接入的具体模式。
