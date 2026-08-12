# Local Color Service V0.2.0 Walkthrough

V0.2 在原有整片调色 API 之上增加镜头级项目流程：

1. `/v1/color/scenes` 检测镜头并分析代表帧。
2. `/v1/color/project-recipe` 生成技术校正与 Look 分离的项目配方。
3. `/v1/color/export` 输出 CC、CCC、CLF 或 CUBE。
4. `/v1/color/project-render` 为各镜头生成 LUT，并用单次 FFmpeg 时间线渲染。
5. 渲染 QC、镜头连续性 QC 和 LUT QC 共同复核结果。

真实 AdaInt 使用 `/v1/color/adaint-lut`，加载官方 FiveK-sRGB 检查点。确定性规则 provider 已准确命名为 `adaptive_lut_deterministic`，不再冒充神经网络。

参考图流程由 `/v1/color/reference-candidates` 生成 A/B/C LUT，再用 `/v1/color/reference-select` 保存选择。默认采用稳定的统计匹配；独立扩散模型运行器可通过环境变量接入。

完整请求示例见 `V0.2_OPERATION_MANUAL.md`，验收与限制见 `V0.2_REVIEW.md`。
