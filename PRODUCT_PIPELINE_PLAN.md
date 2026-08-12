# Local Color Service 产品与 Pipeline 基线

状态：已批准作为 V0.3 及后续版本的产品、架构和验收基线。  
基线日期：2026-08-03

## 1. 核心判断

现有“镜头检测 → 配方/LUT → 时间线渲染 → QC”不推翻，而是作为统一执行内核：

- 传统规则、OCIO、FFmpeg、QC 负责稳定、连续、可交付。
- Look Package 负责安全边界与专业预设。
- AdaInt 负责快速自动增强候选。
- CanonCGT 负责参考图匹配候选。
- AceTone 负责文字或参考条件驱动的创意 LUT 候选。
- Ollama 负责意图理解、受控调度和用户偏好，不直接计算颜色。
- 所有结果必须进入同一个 GradePlan，经过预览、QC、审批后才能正式渲染。

## 2. 产品工作流

### 快速自动调色

面向短视频、批处理和非专业用户。系统完成镜头检测、场景分组、基础校正、候选生成、多帧 QC、自动选择和预览。失败时降低强度，最多重试两次，再回退 Neutral Broadcast。

### 参考图专业匹配

CanonCGT 生成与源画面对齐的目标图，LUT Fitter 将源图到目标图的关系拟合为场景级 3D LUT。用户选择 A/B/C、语言微调、覆盖异常镜头并批准。

### 文字创意调色

AceTone 只生成创意 LUT；镜头检测、基础校正、连续性、安全约束、渲染与 QC 仍由主服务完成。模型稳定前标记为实验模式。

### 自然语言调色助手

Ollama 是所有工作流共用的入口。语言先转换为受 JSON Schema 约束的 GradeIntent，再由规则引擎校验、限幅并转换为受控操作。禁止模型执行任意 FFmpeg、覆盖源文件、绕过 GradePlan 或跳过 QC。

## 3. 统一颜色处理层级

```text
输入媒体
  → Input Transform / 输入归一化
  → 镜头级基础技术校正
  → 镜头匹配
  → 场景级 Creative Transform
  → 镜头级 Trim
  → 高光、饱和度、色域等安全约束
  → Output Transform
  → 时间线渲染与输出 QC
```

场景内优先共享一个 Creative Transform；曝光、白平衡和 Look 强度可逐镜头调整；异常镜头允许退出场景 Look。SAM/局部智能调色当前不在范围内，现阶段不得把全局统计保护描述成语义遮罩保护。

## 4. 核心数据

- Shot：检测得到的单个镜头。
- SceneGroup：共享地点、光线或创意 Look 的一个或多个 Shot。
- GradeIntent：自然语言转换后的结构化意图。
- LookCandidate：Provider 生成的候选、评分、警告和来源。
- TransformAsset：CUBE/CDL/CLF 及色彩空间、模型版本、哈希。
- GradePlan：项目、场景组、镜头校正、Creative Transform、QC 策略与审批状态的唯一事实来源。
- GradePlanRevision：每次人工或语言修改形成的新版本，支持乐观锁和撤销审计。
- PreferenceProfile：仅保存用户明确要求记住的偏好。

## 5. 自动化 Pipeline

1. ffprobe 探测媒体及色彩元数据。
2. 镜头切分，每镜头抽取前/中/后代表帧。
3. 计算亮度、白平衡、饱和度、裁切和异常指标。
4. 使用时间邻近及画面统计自动形成 SceneGroup，并允许人工合并/拆分。
5. 每组选择 Hero Shot，生成逐镜头基础技术校正。
6. 生成三条候选：A 安全专业版，B AdaInt/智能增强版，C 创意版。
7. 在 Hero Shot、场景前中后、最亮最暗及人脸帧上验证。
8. 技术安全作为硬门槛；连续性、内容适配、偏好和风格作为软评分。
9. 高置信度自动选择，中置信度要求确认，低置信度回退安全 Look。
10. 生成预览，用户批准 GradePlan 后正式渲染并再次 QC。

### 时间线交互与性能基线

- 镜头检测后必须生成可视 Shot 时间线；每个 Clip 显示缩略图、SceneGroup、时长、调色来源和 QC 状态。
- 预览分为静帧、Shot 上下文、SceneGroup 和整片四级，人工微调阶段不得强制反复渲染整片。
- Shot 上下文默认包含前后各 1 秒；SceneGroup 预览允许把不连续镜头按原顺序拼接。
- 所有逐段调整仍写入 GradePlan revision；作用域必须明确为 Shot 或 SceneGroup。
- 媒体分析使用缓存代理和批量抽帧；GPU 能力必须实测并显示，不能把 NVENC 编码表述为全链路 GPU 调色。
- GPU 3D LUT 后端未通过颜色精度和元数据验收前，继续使用 CPU 四面体插值。

## 6. CanonCGT Pipeline

```text
选择项目/场景组 → 上传参考图 → 参考图预检 → 选择 Hero Shot
→ CanonCGT 生成目标图 → 多帧源/目标对齐样本
→ LUT Fitter（identity anchor、平滑和边界约束）
→ A 统计安全 / B Canon 标准 / C Canon 接近参考
→ LUT QC 与场景多帧验证 → 人工选择 → 写入 GradePlan
→ 场景预览 → 批准 → 正式渲染
```

CanonCGT 不直接等同于 LUT。若目标图无法由稳定全局 3D LUT 表达，系统必须显示拟合误差并回退统计匹配，不得偷偷改为逐帧生成式渲染。

## 7. AceTone 与 Ollama 边界

AceTone 独立部署，只返回 LUT 和 manifest。RTX 3090 上必须实测加载时间、单/三候选耗时、峰值显存、稳定性、QC 通过率和人工偏好率。

Ollama 采用“两步式”：先输出 GradeIntent JSON，经 Pydantic 和规则校验后由 Orchestrator 执行白名单操作。项目级应用、覆盖人工修改、正式渲染、删除和覆盖文件必须二次确认。

## 8. 版本路线

### V0.3 统一产品底座

- 区分 Shot 与 SceneGroup。
- GradePlan v0.3、revision、审批和撤销审计。
- 统一 Candidate/TransformAsset/Provider 契约。
- 逐镜头强度、曝光、色温编辑和场景改组。
- 正式渲染必须来自已批准 GradePlan。
- 保留 V0.2 API 兼容。

### V0.4 自然语言

Ollama GradeIntent、限幅、Look 推荐、作用域解析、预览/QC 解释和显式偏好记忆。

### V0.5 CanonCGT

独立 Provider、运行环境检测、参考图预检、Hero Shot、多帧 LUT Fitter、A/B/C、拟合误差、QC 和统计回退。

### V0.6 AceTone

独立服务、3B Preview 基准、文字/参考图联合候选、现有 Look 的语言修改；通过真实素材验收后才进入自动候选。

## 9. 不变原则

1. GradePlan 是正式渲染的唯一事实来源。
2. AI Provider 只产生候选，不直接渲染正式输出。
3. 技术安全优先于美学评分。
4. 模型和权重版本、输入空间、LUT、审批和 QC 必须可追溯。
5. 所有失败都必须显式报告 Provider 和回退原因。
6. 用户可以查看、撤销和覆盖自动决策。
