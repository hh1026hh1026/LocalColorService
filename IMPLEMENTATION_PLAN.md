# Local Color Service V0.1 实施计划

## 范围

仅实现 Windows 11 本地、SDR Rec.709 视频调色后端。使用 FastAPI、OpenColorIO、Colour Science、OpenCV、FFmpeg 与 SQLite；不创建前端，不接入 AI 模型，不使用 Python 逐帧渲染完整视频。

## 已有目录审计（2026-08-02）

仓库在本次实施前已有一版代码、测试、合成测试素材和旧验收报告。现有文件将按需修正，不删除用户素材。基线测试为 `13 passed`，但基线实现未满足以下强制项：OCIO Baker、GroupTransform、动态颜色空间选择、每任务独立产物目录、720p 默认预览、完整音频回退与验收测试。

## 实施顺序

1. 环境诊断
   - 先实现并运行 `scripts/doctor.py`。
   - 验证 Python 3.10、FFmpeg/FFprobe、`lut3d`、`zscale`、`h264_nvenc`、`hevc_nvenc`、PyOpenColorIO、ACES Studio Config、ACEScct 与真实 Rec.709 色彩空间。
   - 记录实际选择的 OCIO 配置和颜色空间，任何必需项缺失均失败，不使用虚假回退名称。
2. 核心处理
   - FFprobe 媒体探测；FFmpeg 均匀抽帧；OpenCV/Colour Science 图像统计。
   - 生成带置信度、理由和明确上下限的保守自动建议。
   - 将完整参数、色彩空间、配置标识、素材 SHA-256 和版本写入 `grade_recipe.json`。
   - 在 ACEScct 工作空间内用 OCIO `GroupTransform` 表达曝光、白平衡、lift/gamma/gain、对比度与饱和度。
   - 使用 OCIO `Baker` 烘焙 33 或 65 阶 CUBE LUT。
3. 渲染与质量控制
   - FFmpeg `lut3d=...:interp=tetrahedral`；预览默认 720p。
   - 正式输出优先 `h264_nvenc`，运行失败自动用有效的 `libx264` 参数重试。
   - 音频先 stream copy，容器/编码不兼容时用 AAC 重试。
   - 输出重新 FFprobe；验证非空、可读、时长一帧容差、帧率、音频和 Rec.709 元数据。
4. 任务与 API
   - SQLite 保存 pending/processing/completed/failed、进度、结果和完整错误。
   - 每个 POST 任务建立 `data/jobs/{job_id}/`，保存 `request.json`、`task.log` 和该阶段生成的规范产物。
   - 实现指定的 8 个 FastAPI 路由，所有请求与返回均使用 Pydantic 模型。
5. 验证
   - 补齐指定的九个测试模块、中性 LUT 数值测试和视频验收/失败持久化测试。
   - 在 Python 3.10 环境运行 doctor 与全部 pytest。
   - 启动 Uvicorn，实际访问 `/health`、`/version`、`/docs` 和 `/openapi.json`。
   - 对 `test_assets` 中可识别的体育和 OPC 素材分别执行 analyze、recipe、lut、preview、render、report；若素材不存在，不伪造“真实素材”结果，并在验收报告明确记录阻塞。
6. 交付
   - 更新 `README.md`、`start.bat`。
   - 最后生成 `ACCEPTANCE_REPORT.md`，包含实现、未实现、环境、测试、真实素材、风险和未来 AI 插入点。

## 产物约定

任务目录使用固定名称：`request.json`、`media_info.json`、`analysis.json`、`grade_recipe.json`、`output.cube`、`preview.mp4`、`graded_video.mp4`、`quality_report.json`、`task.log`。各阶段只产生其能够产生的文件；render 任务会产生完整渲染链路所需的配方、LUT、视频和质检报告。
