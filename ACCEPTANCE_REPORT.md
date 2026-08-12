# Local Color Service V0.1 验收报告

> 2026-08-03 更新：本文件记录初始 V0.1 验收。后续颜色流水线修复、23 项测试和 `test.mp4` 最新结果见 `PIPELINE_FIX_REPORT.md`；其中结果优先于本文件的旧数据。

验收日期：2026-08-02（America/Tijuana）  
项目目录：`F:\LocalColorService`

## 结论

V0.1 后端功能、环境诊断、自动测试、Swagger 和两段现有替代素材的端到端流程均已实际运行通过。唯一未满足的交付输入条件是：`test_assets` 中不存在要求的体育视频和 OPC 视频，因此不能声称完成这两类“真实素材”验收；本报告使用仓库现有的两段合成视频完成技术链路替代验收。

## 已实现内容

- FastAPI 指定的 8 个路由，所有请求和响应均由 Pydantic 模型约束。
- FFprobe 媒体探测和 FFmpeg 均匀抽帧。
- OpenCV + Colour Science 的亮度、白平衡、对比度、饱和度和裁切分析。
- 带置信度、理由、明确安全上下限的保守自动建议。
- 包含素材 SHA-256、OCIO 配置标识、实际色彩空间、全部参数与版本的 `grade_recipe.json`。
- 严格 SDR Rec.709 工作流；工作空间为 ACEScct。
- 从实际加载的 ACES Studio Config 枚举并验证颜色空间；未硬编码未经验证的名称。
- 调色参数由 OCIO `GroupTransform` 表达。
- OCIO `Baker` 生成 33 或 65 阶 `iridas_cube` 3D LUT。
- FFmpeg `lut3d` + tetrahedral 插值；没有使用 Python 逐帧渲染完整视频。
- 720p 默认预览。
- 正式渲染优先 `h264_nvenc`，失败后 `libx264`；音频优先 stream copy，不兼容时 AAC。
- H.264 bitstream 写入 Rec.709 primaries/transfer/matrix 元数据。
- 输出重新 FFprobe，检查非空、可读、时长一帧容差、帧率、音频和 Rec.709 元数据。
- SQLite 保存任务状态、结果和完整错误信息。
- 每个任务独立目录 `data/jobs/{job_id}`，保存请求、日志及对应阶段的规范产物。
- `start.bat` 一键诊断并启动 Python 3.10 Uvicorn 服务。

## 未实现内容

- 没有前端（按要求）。
- 没有 AI 模型（按要求）。
- 没有扩展 HDR、非 Rec.709 输入、批处理、用户认证或远程部署（均不属于 V0.1）。
- 未完成体育视频和 OPC 视频的真实素材验收：当前 `test_assets` 只有 `neutral_sample.mp4`、`overexposed_sample.mp4`、`underexposed_sample.mp4` 和 `sample_image.png`，且视频由 `scripts/generate_test_media.py` 生成。

## 环境检查结果

最终执行：`C:\Users\Administrator\.conda\envs\localcolor\python.exe scripts\doctor.py`，退出码 0。

- Python：3.10.20，通过。
- GPU：NVIDIA GeForce RTX 3090 24 GB，通过。
- FFmpeg：`E:\ffmpeg-2025-01-15-git-4f3c9f2f03-essentials_build\bin\ffmpeg.exe`，通过。
- FFprobe：同一发行版目录，通过。
- `lut3d`、`zscale`、`h264_nvenc`、`hevc_nvenc`：全部存在。
- PyOpenColorIO：2.5.2，通过。
- ACES Studio Config：`studio-config-v4.0.0_aces-v2.0_ocio-v2.5`，加载并验证通过。
- 实际枚举选择：`Gamma 2.4 Encoded Rec.709 → ACEScct → Gamma 2.4 Encoded Rec.709`。
- `data/jobs` 写入测试：通过。

注意：用户给定的 `C:\ffmpeg_cuda\bin` 在当前机器不存在，服务按 `.env` 使用已验证的 E 盘 fallback。系统默认 Python 3.12.7 缺少 PyOpenColorIO，因此 `start.bat` 明确使用专用 Python 3.10 环境。

## 测试结果

最终执行：`python -m pytest -q`。

- 结果：`13 passed`，退出码 0，耗时 3.21 秒。
- 覆盖文件：`test_media_probe.py`、`test_frame_sampler.py`、`test_image_analyzer.py`、`test_recipe.py`、`test_ocio_config.py`、`test_lut_baker.py`、`test_render_command.py`、`test_api.py`、`test_quality_control.py`，另有自动建议测试。
- 中性 LUT：33³ 共 35,937 个 RGB 点，最大绝对误差不超过 `1e-5`（当前实际为 0）。
- 视频验收：实际 FFmpeg 渲染验证时长、帧率、音频、可读性、非空和 Rec.709 元数据。
- 失败任务：SQLite `failed` 状态和明确错误信息持久化测试通过。
- 两项非阻塞 warning：TestClient/httpx 的未来弃用提示；Colour Science 未安装可选 matplotlib 绘图依赖。均不影响服务功能。

## Swagger 实测

Uvicorn 实际启动于 `127.0.0.1:8000`。

- `/health`：HTTP 200，`status=ok`，GPU 可用。
- `/version`：HTTP 200，返回 OCIO 2.5.2 与实际 Studio Config。
- `/docs`：HTTP 200，确认包含 Swagger UI。
- `/openapi.json`：HTTP 200，确认包含全部 8 个指定路由。

## 两段现有替代素材处理结果

### neutral_sample.mp4

- analyze：`a7711397b3a14b93b2ccec16ad5b4c61`
- recipe：`b847bd33e33444398b1e4061a384cd3e`
- lut：`6e04360930a6444da940c358329382c0`
- preview：`b11c9b715cc744979cc38f8a9b43e82a`
- render：`ca8479814b5f4ed3a27aa39d18331a60`
- 建议：曝光 0.0 EV、对比度 0.92、饱和度 0.90、置信度 0.85。
- 正式输出：65,735 bytes，H.264 NVENC，AAC 音频，1280×720，25 fps，3.0 s。
- QC：通过；时长差 0.0 s；帧率一致；音频存在；Rec.709 元数据完整。

### overexposed_sample.mp4

- analyze：`2c30255b27e046a09d7391fc7b0895cd`
- recipe：`53d5741ef6ba49cea653e8d6a050a6bc`
- lut：`27d0794a11fc43ee9f1d3e6428bf5787`
- preview：`c33e32793f9c4a009442dab64a675a36`
- render：`c3e2aad847e344bcaa2deed492785c3b`
- 建议：曝光 -0.456 EV、对比度 0.92、饱和度 0.90、置信度 0.85。
- 正式输出：67,854 bytes，H.264 NVENC，AAC 音频，1280×720，25 fps，3.0 s。
- QC：通过；时长差 0.0 s；帧率一致；音频存在；Rec.709 元数据完整。

## 已知风险

- 体育与 OPC 实拍素材缺失；合成测试不能覆盖运动模糊、快速剪辑、混合照明、相机 Log/OPC 特有内容或长节目音频漂移。
- V0.1 明确只接受 SDR Rec.709 语义；错误标记或实际为 Log/HDR 的文件不会被自动识别为另一色彩科学流程。
- NVENC “列出可用”和实际会话可用是两层条件；运行时失败已有 libx264 回退，但速度会下降。
- Gray World 白平衡对大面积单色体育场景可能低估“有意色偏”；因此建议保持保守上下限并输出置信度。
- SQLite 单 worker 适合本地 V0.1，不适合多进程高并发。

## 后续 AI 调色模型插入位置

未来若明确扩展范围，AI 模型只应插入在 `image_analyzer` 输出与 `correction_engine` 建议生成之间：输入抽样帧指标，输出受现有 Pydantic 配方和安全上下限约束的候选参数。后续 OCIO GroupTransform、Baker、FFmpeg 渲染、完整配方持久化和 QC 保持不变，从而继续保证可复现和可审计。V0.1 未包含任何 AI 代码或依赖。
