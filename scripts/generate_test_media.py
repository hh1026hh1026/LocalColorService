"""
Synthetic Test Media Generator for Local Color Service.
Creates H.264 videos with AAC audio and PNG images for testing color analysis, LUT baking, and rendering.
"""

import os
import sys
import cv2
import numpy as np
import subprocess
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_ASSETS_DIR = PROJECT_ROOT / "test_assets"
TEST_ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def get_ffmpeg_executable() -> str:
    env_path = os.getenv("FFMPEG_PATH", r"C:\ffmpeg_cuda\bin\ffmpeg.exe")
    if os.path.exists(env_path):
        return env_path
    fallback_path = os.getenv("FFMPEG_FALLBACK_PATH", r"E:\ffmpeg-2025-01-15-git-4f3c9f2f03-essentials_build\bin\ffmpeg.exe")
    if os.path.exists(fallback_path):
        return fallback_path
    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path
    return env_path


def create_color_chart_pattern(width=1280, height=720, brightness_mult=1.0) -> np.ndarray:
    """
    Generates a synthetic Rec.709 color chart image with color bars, gray ramp, and skin tones.
    """
    img = np.zeros((height, width, 3), dtype=np.uint8)

    # 1. Color Bars (Top half)
    bar_w = width // 7
    colors_bgr = [
        [255, 255, 255], # White
        [0, 255, 255],   # Yellow
        [255, 255, 0],   # Cyan
        [0, 255, 0],     # Green
        [255, 0, 255],   # Magenta
        [0, 0, 255],     # Red
        [255, 0, 0]      # Blue
    ]
    for i, c in enumerate(colors_bgr):
        img[0:height//2, i*bar_w:(i+1)*bar_w] = c

    # 2. Gray Ramp (Bottom half)
    half_h = height // 2
    ramp_1d = np.linspace(0, 255, width, dtype=np.uint8)
    ramp_2d = np.tile(ramp_1d, (half_h, 1))
    img[half_h:height, :] = np.dstack([ramp_2d, ramp_2d, ramp_2d])

    # Apply brightness multiplier
    if brightness_mult != 1.0:
        img_f = img.astype(np.float32) * brightness_mult
        img = np.clip(img_f, 0, 255).astype(np.uint8)

    return img


def generate_assets():
    print("Generating synthetic test assets...")

    # 1. Single Image Asset
    img_path = TEST_ASSETS_DIR / "sample_image.png"
    img = create_color_chart_pattern()
    cv2.imwrite(str(img_path), img)
    print(f"Created: {img_path}")

    ffmpeg_exe = get_ffmpeg_executable()

    # Helper function to render synthetic video with AAC audio
    def create_video(filename: str, mult: float, duration_sec: float = 3.0):
        out_path = TEST_ASSETS_DIR / filename
        temp_raw = TEST_ASSETS_DIR / f"temp_{filename}"

        fps = 25
        num_frames = int(duration_sec * fps)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        writer = cv2.VideoWriter(str(temp_raw), fourcc, fps, (1280, 720))

        frame = create_color_chart_pattern(1280, 720, brightness_mult=mult)
        for _ in range(num_frames):
            writer.write(frame)
        writer.release()

        # Add synthetic AAC audio track via FFmpeg
        cmd = [
            ffmpeg_exe, "-y",
            "-i", str(temp_raw),
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3.0",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            str(out_path)
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        if temp_raw.exists():
            temp_raw.unlink()
        print(f"Created video asset: {out_path} ({duration_sec}s, 25fps, audio)")

    # 2. Neutral Video Asset
    create_video("neutral_sample.mp4", mult=1.0)

    # 3. Under-exposed Video Asset
    create_video("underexposed_sample.mp4", mult=0.35)

    # 4. Over-exposed Video Asset
    create_video("overexposed_sample.mp4", mult=1.80)

    print("All synthetic test assets generated successfully.")


if __name__ == "__main__":
    generate_assets()
