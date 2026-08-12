# Local Color Service 当前流程审查报告

## 1. 审查范围

本次审查基于压缩包中的以下运行产物：

- `project_recipe.json`
- `project_quality_report.json`
- `quality_report.json`
- `request.json`
- `task.log`

压缩包未包含源视频、最终成片、代表帧、LUT 文件本体、FFmpeg 命令或项目源码，因此本报告重点分析：

- 实际运行流程；
- GradePlan/Recipe 数据结构；
- 镜头与场景组关系；
- CanonCGT 候选的接入方式；
- LUT、连续性、肤色与渲染 QC；
- 审批、版本、资产追踪和可复现性；
- 后续产品与工程改造顺序。

本报告不能替代对最终画面的人工视觉审片。

---

# 2. 总体判断

当前系统已经不是单一“套 LUT 工具”，而是具备以下完整链路的内部 Beta：

> 项目配方 → 镜头切分 → 场景分组 → 镜头基础校正 → 项目 Look / 场景 Creative Transform → 每镜头 LUT → 时间线渲染 → 渲染完整性 QC → 项目连续性与 LUT QC → 审批记录

本次任务成功处理了一条约 4 分 18 秒、1920×1080、30 fps、带音频的视频：

- 镜头数：136
- 场景组数：16
- 时间线覆盖：完整，无缝隙、无重叠
- 输出帧数：7732
- 时长差：0
- 帧数差：0
- 音频：保留
- 编码：H.264 NVENC
- 实际处理时间：约 123.3 秒
- 渲染速度：约 2.09 倍实时
- 输出大小：约 371 MiB
- 全局渲染 QC：通过

成熟度建议定义为：

> **可用的内部 Beta / V0.3 流程，但自动审批和 QC 语义尚不足以支持完全无人值守生产。**

---

# 3. 当前实际流程还原

## 3.1 项目配置

本次配方主要信息：

- Recipe Version：`0.3.0`
- Revision：`3`
- Workflow：`reference_assisted`
- Status：`approved`
- 输入：SDR Rec.709
- 工作项目 Look：`cinematic_soft_print`
- 输出：SDR Rec.709
- 批准人：`local-user`
- 未批准配方不可渲染：`allow_unapproved = false`

说明审批门已经进入实际执行链，而不是仅存在于 UI。

## 3.2 镜头分析和场景分组

系统将 257.733333 秒视频切分为 136 个连续镜头：

- 第一个镜头从 0 秒开始；
- 最后一个镜头结束于 257.733333 秒；
- 相邻镜头没有时间缝隙或重叠；
- 全部 136 个镜头都被分入 16 个 SceneGroup；
- 没有遗漏或重复归组。

每个 SceneGroup 具备：

- `scene_group_id`
- `shot_ids`
- `hero_shot_id`
- 可选 `creative_transform`
- 可选 `approved_candidate_id`

## 3.3 镜头基础校正

136 个镜头均有 Base Correction 配方。

实际调整统计：

- 25 个镜头有曝光修正；
- 34 个镜头有非单位 RGB 白平衡增益；
- 4 个镜头有对比度修正；
- 24 个镜头有饱和度补偿；
- 65 个镜头使用了非零 Shot Look Strength；
- 47 个镜头强度为 0.45；
- 18 个镜头强度为 1.0；
- 71 个镜头强度为 0。

自动分析能够识别：

- 欠曝；
- 过曝；
- 平坦动态范围；
- 色偏；
- 曝光提升后的色度补偿。

## 3.4 CanonCGT 接入情况

本次仅 `group_0001` 使用了 CanonCGT Creative Transform：

- 已批准候选：B
- Candidate Strength：0.65
- 最终 Transform Strength：1.0
- Fit Error：0.00551953
- Technical Safety：1.0
- Continuity：0.999941
- Reference Match：0.977922
- Total Score：0.99005

其余 15 个组没有单独 Creative Transform，也没有 Approved Candidate，推测继续继承项目级 `cinematic_soft_print` 或默认路径。

因此本次任务证明了：

> CanonCGT 候选可以被选中、写入配方、批准，并进入最终渲染。

但它尚未证明“整个项目的多场景 CanonCGT 参考匹配”已经形成完整生产闭环。

## 3.5 渲染和最终 QC

最终输出：

- 1920×1080
- 30 fps
- H.264 Main
- 8-bit `yuv420p`
- 约 12.08 Mbps
- AAC 立体声
- 音视频时长和帧数一致
- 使用 `h264_nvenc`

全局分析变化：

| 指标 | 原片 | 成片 | 变化 |
|---|---:|---:|---:|
| 平均亮度 | 0.29397 | 0.32276 | +9.79% |
| 平均饱和度 | 0.15774 | 0.17112 | +8.48% |
| 黑位裁切 | 33.35% | 2.51% | 大幅下降 |
| 高光裁切 | 0% | 1.01% | 增加但仍低于项目上限 |
| 红通道高光裁切 | 0.05% | 2.58% | 明显增加 |

从技术交付角度，输出文件完整、可读、音频保留、帧率和时长正确。

---

# 4. 做得好的部分

## 4.1 时间线完整性很好

136 个镜头完整覆盖源视频，边界连续，没有时间缝隙和重叠。最终输出时长、帧率、帧数和音频完全匹配。

这是自动镜头级渲染最重要的工程基础之一。

## 4.2 Approved GradePlan 已真正成为渲染门

请求中 `allow_unapproved=false`，配方状态为 `approved`，并记录：

- revision；
- approved_at；
- approved_by；
- select candidate event；
- approve event。

说明系统已经具备可审批、可追踪的基础，而不是 AI 结果生成后直接渲染。

## 4.3 CanonCGT 已进入统一配方，不是旁路 Demo

CanonCGT 结果以 `creative_transform` 形式写入 SceneGroup，且包含：

- Provider；
- Candidate ID；
- Fit Error；
- 综合评分；
- Transform 资产路径；
- 来源任务 ID。

这一方向正确。CanonCGT 没有绕过 GradePlan 直接逐帧处理视频。

## 4.4 自动基础校正较为保守

大多数镜头保持曝光、反差和饱和度不变，只对检测到明显问题的镜头调整。

这比“每个镜头都强制自动优化”安全。

## 4.5 渲染性能可用

约 257.7 秒的视频在约 123.3 秒完成正式处理，达到约 2.09 倍实时。

对于本地 GPU 系统，这是一个具备批量生产意义的结果。

## 4.6 已有多层 QC

系统已经包含：

- 文件可读性；
- 时长和帧数；
- 音频保留；
- 元数据；
- 全局裁切；
- 饱和度；
- 色彩保留；
- 镜头间亮度跳变；
- 镜头间白平衡跳变；
- 肤色风险；
- LUT 数值、范围、梯度、单调性和中性轴检查。

架构覆盖面已经比较完整。

---

# 5. 当前主要问题

## P0-1：QC 的“通过”语义过于宽松

`project_quality_report.json` 中：

- `passed = true`
- 13 个相邻镜头亮度跳变；
- 15 个白平衡跳变；
- 5 个肤色风险；
- 48 个镜头 LUT 有警告；
- 其中 11 个 LUT 有突变/潜在 banding 警告。

项目仍然显示通过。

这说明当前 `passed` 更接近：

> 没有致命异常，文件可以输出。

但用户容易理解成：

> 画面质量已经满足自动交付标准。

建议改为分层状态：

```text
render_integrity: PASS
technical_color: PASS_WITH_WARNINGS
scene_continuity: NEEDS_REVIEW
skin_safety: NEEDS_REVIEW
final_decision: NEEDS_REVIEW
```

最终状态建议使用：

- `PASS`
- `PASS_WITH_WARNINGS`
- `NEEDS_REVIEW`
- `FAIL`

自动模式只有在没有中高风险项时才能直接建议批准。

---

## P0-2：当前 LUT QC 对技术校正存在系统性误报

136 个 LUT 中有 48 个警告。

进一步对照配方发现：

- 所有发生曝光或白平衡修正的镜头都出现 LUT 警告；
- 没有曝光和白平衡修正的 88 个镜头，警告率基本为 0；
- 46 个 LUT 提示有较多超范围采样；
- 34 个 LUT 提示中性轴颜色分离；
- 11 个 LUT 提示梯度突变。

这表明当前 QC 把“技术校正 LUT”和“创意 Look LUT”按同一标准检查。

但技术白平衡本来就会移动中性轴；曝光提升也可能产生超 1.0 值。直接用创意 LUT 的中性保持和 `[0,1]` 范围标准检查，会造成大量不可操作的警告。

建议将 Transform 拆分并分别 QC：

```text
Base Correction
  ├── Exposure
  ├── White Balance
  ├── CDL
  └── Tone Curve

Creative Transform
  ├── Project Look
  ├── CanonCGT LUT
  ├── AdaInt LUT
  └── AceTone LUT

Output / Gamut Protection
```

对应 QC：

- Base Correction：检查应用后画面的曝光、白平衡、中性色恢复和裁切；
- Creative LUT：检查平滑度、极端色相偏移、色域、梯度和中性行为；
- 合成后 Shot Transform：检查实际代表帧，不单纯检查 LUT 网格。

不能简单把 48 个警告全部升级为失败；首先要减少系统性误报。

---

## P0-3：SceneGroup 仍未充分解决组内连续性

16 个场景组覆盖了 136 个镜头，但连续性风险分布为：

### 亮度跳变

- 13 个总跳变；
- 6 个发生在同一个 SceneGroup 内；
- 7 个发生在 SceneGroup 边界。

### 白平衡跳变

- 15 个总跳变；
- 14 个发生在同一个 SceneGroup 内；
- 仅 1 个发生在 SceneGroup 边界。

这说明 SceneGroup 当前还没有真正形成稳定的颜色/灯光一致单元，特别是白平衡维度。

此外，多个组恰好有 12 个镜头，表现出固定上限分块的痕迹。当前分组可能包含：

- 语义聚类；
- 连续时间分段；
- 最大镜头数限制；

但对光线和机位关系的约束还不够强。

建议 SceneGroup V2 使用：

- 时间邻近；
- 视觉 embedding；
- 摄像机/构图相似度；
- 亮度与色温分布；
- 主体和人脸特征；
- 来回切换机位的图结构；
- 最大组大小仅作为性能限制，不作为主要分组依据。

同组内白平衡跳变应成为重新分组或 Shot Match 的触发条件。

---

## P0-4：数据模型存在明显的命名和状态漂移

当前配方里：

- `shots` 数组中的 `shot_id` 为 `scene_0001`；
- 同一个对象还有相同值的 `scene_id`；
- 项目 QC 中 `scene_count = 136`，实际是镜头数；
- 16 个真正的场景单元叫 `scene_groups`；
- 136 个 Base Correction 的 `applied` 全部为 `false`；
- 但每个镜头已经生成 LUT，并进入正式渲染；
- `temperature` 和 `tint` 全为 0；
- 但 34 个镜头通过 `rgb_gains` 实际做了白平衡修正。

这会带来：

- UI 显示和实际处理不一致；
- API 调用者误判；
- 后续迁移困难；
- 事件审计不清楚；
- 自动化规则难以判断哪些调整已生效。

建议：

```text
shot_id: shot_0001
scene_group_id: group_0001
legacy_scene_id: scene_0001（仅迁移期保留）
```

把 `applied` 拆成：

```text
generated
selected
baked
rendered
approved
```

白平衡统一表达：

```json
{
  "method": "rgb_gains",
  "rgb_gains": [0.93, 1.0, 1.12],
  "temperature_equivalent": null,
  "tint_equivalent": null
}
```

不要同时保留看似有效但全部为 0 的 temperature/tint 字段。

---

## P0-5：资产和模型版本无法完全复现

当前存在：

- `source_media_hash` 为空；
- Base Correction 中的 `source_media_hash` 也为空；
- CanonCGT `provider_version` 为空；
- Creative Transform 使用绝对 Windows 路径；
- 已批准 LUT 位于另一个 job 目录；
- Recipe 依赖跨任务资产路径；
- 没有记录模型权重 hash；
- 没有记录 LUT 文件 hash。

如果历史任务清理或移动目录，批准过的 GradePlan 可能无法重新渲染。

建议批准时执行“资产冻结”：

```text
Approved GradePlan Revision
  ├── recipe.json
  ├── manifest.json
  ├── source_media_hash
  ├── model/provider versions
  ├── selected LUT/CDL/CLF
  ├── transform hashes
  ├── reference image hash
  └── QC reports
```

Recipe 中引用：

```json
{
  "artifact_id": "transform_xxx",
  "sha256": "...",
  "relative_path": "assets/group_0001.cube"
}
```

不要把临时 job 目录作为长期事实来源。

---

## P1-1：审批覆盖关系不够明确

整个项目状态为 `approved`，但：

- 只有 `group_0001` 有 `approved_candidate_id = B`；
- 其余 15 个 SceneGroup 没有候选批准记录；
- 只有一个 SceneGroup 有 Creative Transform。

如果其余组明确继承项目 Look，这可以是合理的；但配方应该显式记录：

```json
{
  "creative_source": "project_look_inherited",
  "approval_state": "approved_by_project",
  "fallback_used": false
}
```

否则无法区分：

- 用户确认使用项目 Look；
- 尚未选择候选；
- Provider 未运行；
- Provider 失败后回退；
- 场景不需要 Creative Transform。

---

## P1-2：项目 Look 与镜头基础配方层级混杂

项目级：

```text
project_look = cinematic_soft_print
```

但每个 Base Correction 内又记录：

```text
look_id = neutral_broadcast
look_strength = 0
```

Shot 层又有：

```text
look_strength = 0 / 0.45 / 1.0
```

这使得 Look 来源和强度难以理解。

建议严格分层：

```text
base_correction
  仅包含技术校正

project_look
  项目默认创意风格

scene_creative_transform
  场景级 Provider 结果

shot_trim
  每镜头强度和补偿
```

Base Correction 中删除 `look_id` 和 `look_strength`。

---

## P1-3：Shot Match 实际没有进入本次流程

136 个镜头的 `shot_match` 全部为 `null`。

但项目仍出现：

- 6 个组内亮度跳变；
- 14 个组内白平衡跳变。

这表明系统目前主要依赖逐镜头基础校正，尚未利用 Hero Shot 做真正的组内匹配。

建议 SceneGroup 生成后执行：

```text
Hero Shot
  ↓
组内每镜头亮度、色温、肤色和色彩分布比较
  ↓
只生成低维、受约束的 Shot Match Trim
  ↓
组内连续性复检
```

Shot Match 不应生成另一张完全独立的 AI LUT，而应优先输出：

- Exposure Trim；
- RGB gain / temperature trim；
- Saturation trim；
- 小幅 Tone Curve；
- 必要时的弱统计色彩匹配。

---

## P1-4：开场黑场、转场和画面黑边需要独立识别

源视频全局黑位裁切为 33.35%，输出下降到 2.51%。

第一个镜头：

- 时间：0–3.366667 秒；
- 中位亮度：0；
- 自动曝光：+0.8 EV；
- 同时使用 CanonCGT；
- 最终 LUT 被警告有突变风险。

这可能意味着：

- 黑场或淡入；
- 片头字幕；
- Letterbox；
- 有意压黑的镜头；
- 真正欠曝镜头。

仅凭报告无法确认，但当前算法有把“意图黑场”当欠曝并抬亮的风险。

建议在自动曝光前增加：

- 黑帧检测；
- 淡入淡出检测；
- Letterbox / Pillarbox 检测；
- 字幕和片头图形分类；
- 极低内容占比检测；
- 转场镜头标记。

这些镜头默认：

```text
base_grade_policy = preserve
creative_policy = inherit_or_bypass
```

而不是直接执行 +0.8 EV。

---

## P1-5：肤色风险必须进入人工复核清单

本次有 5 个肤色风险：

| Shot | 时间范围 | SceneGroup | 风险 |
|---|---|---|---|
| scene_0032 | 00:00:32.033–00:00:33.067 | group_0006 | Hue/Luma shift |
| scene_0047 | 00:00:55.267–00:00:56.200 | group_0007 | Hue/Luma shift |
| scene_0049 | 00:00:57.933–00:00:59.533 | group_0008 | 调色后未再检测到源人脸 |
| scene_0097 | 00:02:32.000–00:02:34.300 | group_0013 | Hue/Luma shift |
| scene_0103 | 00:02:47.733–00:02:50.567 | group_0013 | Hue shift 较大 |

在暂不引入 SAM 的情况下，建议提供：

- QC 时间码跳转；
- 原片/成片人脸裁切对比；
- “降低 Look 强度”快捷操作；
- “保持基础校正、关闭 Creative Look”快捷操作；
- 人工标记误报；
- 误报反馈进入阈值调优。

---

## P1-6：当前输出适合作为交付文件，不适合作为高质量母版

本次输出为：

- H.264 Main；
- 8-bit；
- yuv420p；
- 约 12 Mbps。

用于网页、普通交付和预览没有问题。

对于后续再剪辑、进入达芬奇精调或保留高质量母版，建议增加：

```text
Preview
H.264 / 720p 或 1080p

Delivery
H.264 / HEVC

Master
ProRes 422 HQ / DNxHR HQX / 10-bit HEVC
```

特别是高强度 Look、渐变、暗部和天空画面，8-bit 4:2:0 更容易暴露 banding。

---

## P2-1：任务日志过于简略

当前日志只有：

```text
job created
processing project_render
completed
```

建议至少记录：

- GradePlan revision；
- Approved status；
- 输入和输出 artifact ID；
- Shot 数、SceneGroup 数；
- LUT 数；
- FFmpeg/NVENC/libx264 实际选择；
- FFmpeg 命令摘要或 filter graph hash；
- GPU 名称；
- 编码速度；
- 峰值显存；
- 每阶段耗时；
- stderr 尾部摘要；
- QC 采样帧数量与时间；
- 自动回退记录；
- 取消状态。

---

# 6. 建议的新 PIPELINE

```text
01 创建项目
02 媒体探测和输入色彩空间确认
03 检测黑边、黑帧、淡入淡出、图形和字幕镜头
04 Shot Detection
05 每镜头抽取多张代表帧
06 SceneGroup V2 聚类
07 用户可选地修正分组
08 选择 Hero Shot
09 生成镜头 Base Correction
10 生成组内 Shot Match Trim
11 创建 GradePlan Draft
12 选择工作模式
   ├── 自动专业 Look
   ├── AdaInt 增强
   ├── CanonCGT 参考图
   └── AceTone 创意实验
13 生成 SceneGroup LookCandidate
14 将候选物化为 TransformAsset
15 在组内多镜头、多帧应用候选
16 分层 QC
   ├── Base Correction QC
   ├── Creative Transform QC
   ├── 合成后画面 QC
   └── SceneGroup 连续性 QC
17 自动排序或用户选择
18 写入新的 GradePlan Revision
19 生成场景和整片预览
20 人工或自然语言微调
21 再次 QC
22 批准 Revision
23 冻结全部资产和 Hash
24 单次时间线正式渲染
25 最终文件完整性和画面 QC
26 导出视频、Recipe、LUT/CDL/CLF 和报告
```

---

# 7. 推荐改造顺序

## 第一阶段：修正状态、Schema 和资产追踪

建议 3–5 天。

完成：

- `shot_id` / `scene_group_id` 命名迁移；
- `scene_count` 改为 `shot_count`；
- 清理 Base Correction 中的 Look 字段；
- `applied` 拆分为 generated/selected/baked/rendered；
- 白平衡字段统一；
- Source、Reference、Model、LUT hash；
- Provider Version；
- 绝对路径改为 Artifact ID + 相对路径；
- Approved Revision 资产冻结；
- 最终 QC 改为四级状态。

验收标准：

- 任一历史批准项目可以在清理临时任务后重新渲染；
- UI 参数与实际 LUT 完全一致；
- 没有歧义的 `scene` / `shot` 命名。

## 第二阶段：重构 QC

建议 4–7 天。

完成：

- Base / Creative / Combined 三层 QC；
- 去除技术校正导致的系统性 LUT 误报；
- 同组和跨组连续性分别判断；
- 时间码化 QC 结果；
- 肤色风险快捷复核；
- 黑边和转场排除；
- 自动模式的 PASS/WARN/REVIEW/FAIL 决策。

验收标准：

- 技术校正镜头不再 100% 触发泛化 LUT 警告；
- 同组连续性问题可直接跳转和修正；
- `passed=true` 不再掩盖需要人工检查的问题。

## 第三阶段：SceneGroup V2 和 Shot Match

建议 1–2 周。

完成：

- 基于视觉、时间、亮度、白平衡和机位关系的分组；
- SceneGroup 编辑 UI；
- Hero Shot 评分解释；
- 组内 Shot Match；
- 分组后连续性复检；
- 特殊镜头自动退出组 Look。

验收标准：

- 组内白平衡跳变显著少于当前 14 个；
- 固定 12 镜头分块不再主导分组；
- 用户可快速合并、拆分和移动镜头。

## 第四阶段：完善 CanonCGT 半自动流程

建议 1 周。

完成：

- 单 SceneGroup + 单 Hero Shot + 单参考图；
- 参考图预检；
- CanonCGT 输出目标图；
- CDL + Tone + Residual 3D LUT Fitter；
- 多帧拟合和误差报告；
- A 安全 / B 标准 / C 风格候选；
- 组内多镜头验证；
- 回退到统计匹配；
- Provider 和权重 Hash。

验收标准：

- CanonCGT 不依赖临时任务路径；
- LUT 拟合误差和适用范围可解释；
- 选定候选可以稳定应用整个 SceneGroup。

## 第五阶段：输出和可运营性

建议 3–5 天。

完成：

- Preview / Delivery / Master 三类输出；
- 10-bit 母版；
- 详细阶段日志；
- 磁盘占用和自动清理；
- 任务取消和恢复；
- 性能指标面板。

---

# 8. 近期最应该优先处理的 10 项

1. 将最终 QC 改成 PASS / WARN / REVIEW / FAIL。
2. 拆分技术校正 LUT QC 与创意 LUT QC。
3. 清理 Shot/Scene 命名漂移。
4. 修复 `applied=false` 与实际渲染不一致。
5. 统一白平衡的 RGB gain 与 temperature/tint 表达。
6. 给 Source、Reference、Provider、Model 和 LUT 增加 Hash。
7. 批准时冻结所有 TransformAsset。
8. 增加黑帧、淡入淡出和 Letterbox 检测。
9. 对 14 个组内白平衡跳变启用 Shot Match 或重新分组。
10. 提供肤色风险和 LUT 高风险镜头的时间码复核列表。

---

# 9. 最终结论

当前系统最值得肯定的是：

> 已经形成了真正的项目级镜头调色、审批、渲染和 QC 闭环，CanonCGT 也已经以 Provider 的形式进入统一配方，而不是独立演示。

目前最大的短板不是缺少新的 AI 模型，而是：

> **SceneGroup 的颜色一致性还不够强，QC 对不同类型 Transform 缺乏角色感知，审批和资产追踪尚未达到完全可复现。**

短期最优策略不是继续增加模型，而是先完成：

```text
Schema 清理
+ SceneGroup V2
+ Shot Match
+ 分层 QC
+ Approved Asset Freeze
```

完成后，再扩展 CanonCGT 多场景、Ollama 自然语言和 AceTone，系统会更像可靠的生产平台，而不是不断叠加模型的实验工具。
