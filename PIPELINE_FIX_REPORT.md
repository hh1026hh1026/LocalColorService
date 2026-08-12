# Pipeline 完整修复报告

日期：2026-08-03  
版本：0.1.3

## 根因

1. 生产 LUT 路径绕过了 OCIO Baker，改为 NumPy 手工采样和写 CUBE。
2. ACEScct 曝光错误地直接乘以 `2^EV`；手写 ACEScct 正反变换公式也不正确，导致高光裁切和色度损失。
3. AdaInt 仿真 provider 使用偏激进饱和度和 RGB 色偏；Reinhard 缺少参考图时曾失败或使用不可靠的内置纯色色板。
4. 预览回退到 540p；音频只支持 copy；NVENC 回退命令通过原地修改参数，可靠性不足。
5. SQLite 任务领取不是原子操作，worker 中断后任务可能永久停在 processing。

## 修复

- 恢复 `OCIO GroupTransform → OCIO Baker(iridas_cube)`，删除生产路径中的手写 ACEScct 数学。
- 严格枚举实际 ACES Studio Config 色彩空间；当前为 `Gamma 2.4 Encoded Rec.709 → ACEScct → Gamma 2.4 Encoded Rec.709`。
- 配方写入实际 OCIO 配置、统一 0.1.3 版本和确定性 SHA-256 recipe hash。
- 所有自动 provider 经过统一安全边界。
- AdaInt 仿真取消 RGB 偏色，使用实测标定的色度保持参数；Reinhard 无参考图时回退传统分析。
- 恢复 720p 默认预览。
- FFmpeg 使用明确的重试矩阵：NVENC/copy → NVENC/AAC → libx264/copy → libx264/AAC。
- 输出固定写入 limited-range Rec.709 H.264 bitstream 元数据。
- QC 新增源片/输出平均饱和度与亮度比，以及严重失色拦截。
- SQLite 原子领取任务；启动时恢复可恢复任务，并将缺少目录的悬挂任务标记为明确失败。
- 每次 FastAPI lifespan 创建独立 worker，避免线程对象无法再次启动。
- 上传按 SHA-256 去重，避免同一大文件反复占用磁盘。

## 自动测试

- doctor：全部通过。
- pytest：`23 passed, 1 warning`。
- warning 为 Starlette TestClient/httpx 未来弃用提示，不影响运行。

## test.mp4 真实链路验收

素材：2560×1440、24 fps、137.531791 秒、Rec.709、AAC 音频。

- analyze：`01dfc7081d04`
- recipe：`8b0e958c1fdf`
- LUT：`ccbbbc6760a2`
- 720p preview：`2cfeea1a31c4`
- NVENC render：`69199c08f1e4`
- recipe hash：`81b1aefd88b18f79083c7b51a38e7649991636911be44782631e2c6d60287f42`

最终配方：曝光 +0.20 EV、对比度 1.04、饱和度控制值 1.15、色温 0，无 RGB 人工偏色。

验收结果：

- Preview QC：通过；颜色保留 0.9650×，亮度 1.0602×。
- Render QC：通过；颜色保留 0.9738×，亮度 1.0617×。
- 时长差：0 秒。
- 帧率：24 fps，保持一致。
- 音频：存在，stream copy 成功。
- Rec.709 primaries/transfer/matrix：完整。
- 输出：`data/jobs/69199c08f1e4/graded_video.mp4`。
- 最终 QC 无 warning、无 error。

## 说明

`adaint_neural` 当前是确定性的 AdaInt 风格配方 provider，并未加载训练权重。若后续接入真实模型，模型输出仍应只生成候选 GradeRecipe，并继续经过本次建立的安全边界、OCIO Baker、FFmpeg 和 QC 主链路。
