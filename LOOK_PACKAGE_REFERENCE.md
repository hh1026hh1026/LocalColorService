# AI LUT、镜头调色与 Look Package 关系说明

更新日期：2026-08-03

## 1. 当前模块关系

当前 V0.2 有三条并行工作流：

```text
镜头项目：素材 → 镜头分析/技术校正 → Look Package → 每镜头 LUT → 时间线渲染 → QC
AdaInt：  素材 → 代表帧 → AI 生成全局 LUT → LUT 预览 → QC
参考图：  素材 + 参考图 → A/B/C 候选 LUT → 人工选择 → LUT 预览 → QC
```

镜头项目中的 LUT 是“技术校正 + Look Package”合成后的执行产物；AdaInt LUT 是模型从代表帧直接推理出的创意/增强结果。两者都是 CUBE，但来源和职责不同。

V0.2 不会自动把 AdaInt LUT 叠加到 Look Package 上。任意串联两个 LUT 容易产生重复对比度、重复饱和、双重白平衡、高光二次压缩和色域越界。后续若要合并，正确做法是把 AI 结果作为可调强度的项目 Look 层，在 ACES 工作空间内与技术校正组成一次变换，再统一烘焙和 QC。

## 2. 设计依据

这些 Look 是本项目的保守工程预设，不是 Academy、ITU、EBU 发布的官方 Look，也不等于某种胶片库存的测量仿真。

采用的权威边界：

- ACES Look Transform 指南：Look 是输出变换之前的全画面系统性创意变换，应补充而非替代镜头调色，并尽量保存动态范围和宽色域。所有九套 Look 在进入创意处理前启用配置内置的 ACES 1.3 Reference Gamut Compression。
- ITU-R BT.709-6：确定当前 SDR HDTV 输出的色度学/信号基础。
- EBU R 103：用于合法电平和 RGB/Y 越界检查；不在中间处理阶段粗暴截断颜色。

来源：

- https://docs.acescentral.com/system-components/look-transforms/
- https://docs.acescentral.com/system-components/look-transforms/specification/
- https://www.itu.int/rec/R-REC-BT.709/en
- https://tech.ebu.ch/publications/r103

## 3. 九套 Look

| Look | 适用内容 | 核心特点 | 必须保护 |
|---|---|---|---|
| Neutral Broadcast | 新闻、访谈、纪录片 | 中性、稳定、肤色自然、广播安全 | 肤色、白色、合法电平 |
| Clean Commercial | 产品、企业片、教育 | 明亮、干净、白色纯净、适度清晰 | 产品颜色、白背景 |
| Cinematic Soft Print | 剧情、宣传片 | 柔和肩部、厚实中间调、轻微暖高光 | 高光、红色、肤色 |
| Restrained Teal–Amber | 预告、科技、动作 | 冷阴影、暖肤色倾向、低强度色彩分离 | 肤色、天空、黑位 |
| Warm Memory | 文旅、人物、回忆 | 暖中高光、轻抬暗部观感、降低蓝绿存在感 | 白色、中性灰 |
| Cold Thriller | 悬疑、工业、夜景 | 冷阴影、环境低饱和、肤色相对中性 | 人脸、暗部细节 |
| Sports Vivid | 足球、篮球、赛事 | 清晰、较高局部对比观感、队服颜色鲜明 | 草地、肤色、LED |
| Stage Mixed Light | 演出、OPC、舞台 | 抑制洋红/绿色过冲、保留彩灯氛围、软化 LED 高光 | LED、高饱和灯光、肤色 |
| Day for Night | 剧情特效 | 降曝光、冷环境、压低暖背景、保留月光方向 | 高光、肤色、天空 |

## 4. V0.2 参数与边界

| Look ID | 曝光 | 对比度 | 饱和度 | 色温 | 高光柔化 | 色彩密度 | 暗部 RGB 偏置 |
|---|---:|---:|---:|---:|---:|---:|---|
| neutral_broadcast | 0.00 | 1.00 | 1.00 | 0.0 | 0.12 | 0.00 | 0 / 0 / 0 |
| clean_commercial | +0.08 | 1.06 | 1.04 | +1.5 | 0.28 | 0.08 | 0 / 0 / 0 |
| cinematic_soft_print | 0.00 | 1.08 | 0.96 | +4.0 | 0.65 | 0.25 | -0.004 / 0 / +0.006 |
| restrained_teal_amber | -0.03 | 1.10 | 0.96 | +3.0 | 0.45 | 0.12 | -0.010 / +0.002 / +0.014 |
| warm_memory | +0.06 | 0.96 | 0.88 | +8.0 | 0.48 | 0.08 | +0.004 / 0 / -0.006 |
| cold_thriller | -0.12 | 1.10 | 0.78 | -10.0 | 0.38 | 0.10 | -0.008 / +0.002 / +0.016 |
| sports_vivid | +0.08 | 1.10 | 1.12 | 0.0 | 0.30 | 0.12 | 0 / 0 / 0 |
| stage_mixed_light | 0.00 | 1.02 | 0.92 | 0.0 | 0.70 | 0.06 | 0 / 0 / 0 |
| day_for_night | -1.00 | 1.12 | 0.68 | -16.0 | 0.55 | 0.08 | -0.012 / +0.001 / +0.026 |

参数以强度 100% 表示，项目镜头可通过 `look_strength` 从 0～1 插值。所有数值都是测试起点，需要用真实素材评分后继续校准。

## 5. “必须保护”的真实含义

目前正值 `gamut_protection` 会启用 ACES 1.3 Reference Gamut Compression，并与 LUT/渲染 QC 一起工作；其 0～1 数值也保留为未来的保护强度元数据。`skin_protection` 会保存在配方和审计数据中，但人脸、天空、草地、LED 等语义区域尚未生成实际遮罩。因此：

- “必须保护”当前是设计约束、参数强度约束和 QC 目标。
- 它不是逐像素语义隔离承诺。
- Stage Mixed Light、Sports Vivid、Day for Night 等依赖局部区域的 Look，仍需人工检查。
- 后续应通过肤色检测、LED/天空分类或 SAM 2 遮罩实现真正局部保护。

## 6. 推荐使用原则

1. 先完成镜头检测和技术校正，再选择 Look。
2. 第一次测试使用 60%～80% Look 强度比满强度更稳妥。
3. Neutral Broadcast 可作为基准版本，与其他 Look 做 A/B 对比。
4. Day for Night 只是起点，不能替代天空、窗户、人物和月光方向的局部处理。
5. Stage Mixed Light 不应全局中和所有彩灯；先保留舞台意图，再控制 LED 越界。
6. 不直接串联 AdaInt LUT 与 Look Package；若确需比较，分别生成预览后人工选择。
