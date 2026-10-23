# 导航证据

正常 `run` 自动保存 agent 寻找页面时的图片、规划、定位与动作证据，不需要新的项目配置。
运行命令、项目任务及模型选择不变。证据位于内部 `run.json` 的 `artifacts_directory` 下：

```text
planning-evidence/
  trace.jsonl      按保存顺序排列的结构化记录
  screenshots/    SDK 图片存储保存的实际图片字节
```

每次 `aiAct` 使用独立 `invocation_id`；SDK 的 `execution_id`、`task_id` 直接关联任务。
`plan_index` 表示所属规划轮次。不能用相近时间、相同点击坐标或相似图片替代任务关联。
这些文件属于内部运行证据，不计样本配额，不加入项目声明的交付文件，采集目录仍只有一张配套截图。

```mermaid
flowchart LR
    Device[设备截图和窗口事实] --> Context[SDK 上下文图片与缩放比例]
    Context --> Request[实际模型请求中的图片]
    Request --> Plan[规划动作与可用的原始坐标]
    Plan --> Locate[定位结果与命中来源]
    Locate --> Dispatch[原生动作调用参数]
    Dispatch --> After[动作后的 SDK 观察]
    Plan --> Trace[同一 SDK 任务关联的内部记录]
    Locate --> Trace
    Dispatch --> Trace
    After --> Trace
```

## 图片与坐标

| 记录 | 能证明的事实 |
| --- | --- |
| `observation` | 本机收到的设备截图、窗口事实、观察标识和状态版本 |
| `context` | SDK 截图标识、图片尺寸、`shot_size` 和 `shrunk_shot_to_logical_ratio` |
| `model_request` | 实际发出请求中的图片顺序、原始编码、尺寸及 SHA-256，直接关联正在执行的 SDK 任务 |
| `sdk_task` 的 `plan` | 标准协议解析出的动作、原始坐标及协议声明；不保存原始模型正文 |
| `sdk_task` 的 `locate` | 最终定位结果、定位描述及 `hit_by`，区分规划直接命中与额外模型定位 |
| `tool_call` | SDK 已转换到设备逻辑像素的注册工具参数和状态版本 |
| `action_dispatch` | 实际调用原生动作时传入的参数、调用开始及返回状态 |
| `action_observation` | 同一动作任务的后继截图，缺失时明确标记 `unavailable` |

图片记录包含相对内部运行目录的文件路径、字节数、真实尺寸及 SHA-256。
设备截图、SDK 上下文图片、实际请求图片分别保存；它们可能有不同尺寸或编码，不能相互冒充。
图片内容相同时复用 SDK 图片存储，记录仍保留各自的来源与关联。

标准模型协议的 `prompt_spec` 保存公开协议声明，原始数值必须按该声明解释。
自定义模型适配器没有公开原文解析接口时，只记录 SDK 已归一化的截图像素结果和公开坐标声明，
同时明确标记原始坐标不可用；不从最终坐标反推原始模型输出。由内部思考文本生成的定位描述不保存。
定位记录使用 SDK 实际选择的 `locate` 角色解析协议；`insight` 角色用于理解与提取，不能替代定位角色。
两个角色都继承默认模型时结果相同，独立配置后可能使用不同的坐标格式。

`native_returned` 只表示原生回调返回。`os_delivery_confirmed=false` 和 `ui_effect=not_evaluated`
明确表示这不是操作系统送达回执，也不是点击有效的结论。
后继图片仅取自动作任务，规划任务自身的截图不冒充动作后的观察。
诊断不增加重复点击阈值、不修正坐标、不替代现有页面稳定性或最终图片验收。

## 规划与独立定位

当前采集从每次 `aiAct` 开始固定使用 Midscene 1.16.5 的 `effort="deepThink"`。
标准规划器只提供目标描述，独立 `Locate` 请求在截图中定位，再经过原有窗口与任务范围检查后派发动作。
即使规划回复附带粗坐标，SDK 也只保留描述，不将它作为直接点击位置。
规划和定位仍可使用同一个默认模型，不要求新增模型配置或账号。

```mermaid
flowchart LR
    Plan[deepThink 标准规划只取目标描述] --> Locate[独立 Locate 模型请求]
    Locate --> Scope
    Scope{核对当前窗口与输入范围} -->|通过| Native[调用原生动作]
    Scope -->|拒绝| Stop[终止并保留失败证据]
```

该选项也启用 SDK 的子目标规划，并将规划保留的历史图片上限从一张改为两张。
标准规划请求不再要求定位用的原始图像细节；实际请求图片仍按 SDK 处理结果留存诊断证据。
每个需要定位的动作会增加独立定位请求，通常增加用量与延迟；实际费用仍以运行的请求用量记录为准。
这是固定执行策略，没有失败后切换、自动重试或新增配置开关；`deepLocate` 裁图能力没有启用。
`effort` 不改变 provider 的 `thinking`、`reasoning_effort` 或其他模型账号参数，
这些参数仍由既有模型配置决定。依据为[官方规划模式说明](https://midscenejs.com/reference/#deepthink-planning-mode)、
[固定版本的规划条件](https://github.com/web-infra-dev/midscene/blob/v1.16.5/packages/core/src/agent/tasks.ts#L557-L562)。

该行为适用于 SDK 的标准规划器，包括当前使用的 DeepSeek。SDK 对自定义规划适配器会警告并忽略
`effort="deepThink"`，例如 UI-TARS；不能据此声称自定义规划器也实现独立定位。
旧运行中的 `Locate source=plan` 表示直接采用规划点，没有额外定位请求；新标准规划运行应记录
`Plan → Locate → Plan` 请求链，定位的 `source=ai` 与请求角色由实际 SDK 任务确定。

项目中的路径停止条件交给规划阶段处理；独立定位只解决当次目标在哪里，不决定是否继续任务。
SDK 支持未完成目标时报告失败并退出，采集器随后保留失败状态；成功采集仍必须取得候选并通过验收。
诊断能证明发生了哪些动作、图像是否变化，不能仅凭原生调用返回就认定任务停止条件未触发。

## 独立定位的坐标诊断

定位原文仍由当前模型的 SDK 公开协议解析。SDK 1.16.5 的 DeepSeek 提示会把已有方括号的
坐标 schema 和示例再包一层数组，产生 `[[x,y]]`；SDK 自身可以解码这种值。
诊断读取单个数值向量及其单个外层数组，保存其中原始数值，不把外层数组误判为无效坐标。
多个候选向量、非数值或非有限值仍拒绝，不静默选取第一个候选。
依据为[固定版本 DeepSeek 定位协议](https://github.com/web-infra-dev/midscene/blob/v1.16.5/packages/core/src/ai-model/models/deepseek/locate-protocol.ts)
与[SDK 坐标值解析](https://github.com/web-infra-dev/midscene/blob/v1.16.5/packages/core/src/ai-model/shared/model-locate-result/parse.ts)。

诊断不重算或替代 SDK 的点击位置。截图像素结果、SDK 上下文的缩放比例和原生动作参数分别记录；
缩放及取整顺序继续由 SDK 执行，原有窗口范围检查不变。
投影失败时同步保存 `diagnostic_failure`，包含 SDK 执行及任务标识、任务状态、截图标识和固定阶段码
`element_geometry`、`raw_protocol` 或 `raw_coordinates`，随后仍中止运行。
同时保留 SDK 已返回的有限数值几何和原文是否存在，避免原文提取错误连带丢失已经得到的定位结果。
该记录不包含模型全文、原始错误消息或业务输入正文；非法几何字段不会作为有效数值保存。

百度运行 `8b89c264-6980-4b50-a43c-2c8a3223946a` 的冻结证据止于首次 Locate 模型请求返回，
没有终态定位结果或原始回复。实际请求图片与最后上下文图片均为 `3120×2080`，摘要相同，比例为 `1`。
SDK 双层坐标可以离线复现同一错误，但不能据此补写该历史运行的原始响应或宣称已关闭弹窗。

## 保存范围与失败

记录由已安装 Midscene 的公开进度、执行快照、模型客户端工厂和图片存储接口产生。
完整 SDK 报告、原始执行转储和动作缓存仍关闭；不记录请求头、认证选项、完整提示词、
完整模型回复或独立 Input 正文字段。定位描述只保留公开定位字段；已知模型密钥字面值会被过滤。

内部图片保留实际导航画面，可能包含屏幕中的账号或输入内容。项目的采后截图打码开关只处理
正式采集截图，不改变导航图片或已经发出的模型请求；内部导航证据不属于脱敏交付。

证据写入失败会中止运行，动作派发前和验收前均等待写入，结束时仍完成本地收尾。
中断后保留已保存的证据，缺图和未知界面效果保持明确状态。
过去未记录的模型图片或原始坐标无法离线补造；这项能力需要下一次正常运行产生新证据。
