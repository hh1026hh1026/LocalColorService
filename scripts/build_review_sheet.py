"""Build a human review sheet for the shots the system is least sure about.

Two categories go in, because they are the two where the automatic pass has
already admitted it could not finish the job:

``highlight_limited``
    The measurement asked for more exposure than the highlight headroom allowed,
    so the subject was left darker than intended in order not to blow the
    background. A global correction cannot solve these; they need a local one.
    Only a person can say whether the compromise is acceptable.

``skin_hue_shift``
    Skin hue rotated further than the tolerance. The number says how far; it
    cannot say whether it looks wrong.

Output is a single self-contained HTML file - frames are embedded, so it can be
mailed around or opened on a machine that has none of this installed. Each
reviewer records a verdict and exports a CSV.

Usage:
    python scripts/build_review_sheet.py --render-job a0dbcffde053
    python scripts/build_review_sheet.py --render-job <id> --max-shots 40 --width 560
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np

REPOSITORY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY))

from color_core.frame_sampler import sample_frames_at  # noqa: E402


def data_dir() -> Path:
    configured = os.getenv("DATA_DIR")
    if configured:
        return Path(configured)
    env_file = REPOSITORY / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("DATA_DIR="):
                return Path(line.split("=", 1)[1].strip())
    return REPOSITORY / "data"


def timecode(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours, remainder = divmod(total, 3600.0)
    minutes, secs = divmod(remainder, 60.0)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def load_scene_analysis(root: Path, scene_job_id: str) -> dict:
    connection = sqlite3.connect(f"file:{root / 'local_color.db'}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT result_data FROM jobs WHERE job_id = ?", (scene_job_id,)
    ).fetchone()
    connection.close()
    if not row or not row[0]:
        return {}
    return json.loads(row[0]).get("scene_analysis", {})


def collect_flagged(job_dir: Path, root: Path) -> tuple[list[dict], dict]:
    quality = json.loads((job_dir / "project_quality_report.json").read_text(encoding="utf-8"))
    recipe = json.loads((job_dir / "project_recipe.json").read_text(encoding="utf-8"))
    scenes_raw = load_scene_analysis(root, recipe.get("scene_analysis_job_id", ""))
    scenes = {item["scene_id"]: item for item in scenes_raw.get("scenes", [])}
    shots = {shot.get("shot_id") or shot["scene_id"]: shot for shot in recipe.get("shots", [])}

    flagged: dict[str, dict] = {}

    # Large exposure corrections: the one category validated against people.
    # Measured on a 45-shot review, this selected 14 of 17 shots the reviewer
    # wanted reworked - precision 0.82, against 0.56 for the skin-hue category
    # it replaces.
    for entry in quality.get("large_exposure_corrections", []):
        shot_id = entry.get("shot_id") or entry.get("scene_id")
        flagged.setdefault(shot_id, _blank(shot_id, entry, shots, scenes))["reasons"].append({
            "kind": "large_exposure",
            "headline": f"自动曝光调整了 {entry.get('applied_ev', 0):+.2f} EV",
            "detail": (
                f"测量请求 {entry.get('requested_ev', 0):+.2f} EV  锚点={entry.get('anchor', '-')}"
                + ("  高光受限" if entry.get("highlight_limited") else "")
            ),
            "question": "调整幅度这么大，结果可以接受吗？还是需要局部处理？",
        })

    # Skin hue: only the unarguable rotations reach the sheet now. At the old
    # 6-degree gate this category was right about half the time (precision 0.56),
    # which is not worth a reviewer's attention.
    for risk in quality.get("skin_tone_risks", []):
        if risk.get("category_detail") == "skin_clipped":
            shot_id = risk.get("shot_id") or risk.get("scene_id")
            flagged.setdefault(shot_id, _blank(shot_id, risk, shots, scenes))["reasons"].append({
                "kind": "skin_clipped",
                "headline": "肤色被推到显示范围顶端，细节已丢失",
                "detail": f"ΔL*={risk.get('skin_delta_lightness', 0):+.1f}",
                "question": "人脸/皮肤是否已经死白？",
            })
            continue
        if risk.get("category_detail") != "skin_hue_shift":
            continue
        shot_id = risk.get("shot_id") or risk.get("scene_id")
        flagged.setdefault(shot_id, _blank(shot_id, risk, shots, scenes))["reasons"].append({
            "kind": "skin_hue_shift",
            "headline": f"肤色色相旋转 {risk.get('skin_hue_rotation_deg', 0):+.1f}°",
            "detail": (
                f"ΔH*={risk.get('skin_delta_hue', 0):+.2f}  "
                f"ΔL*={risk.get('skin_delta_lightness', 0):+.1f}（不计入判定）  "
                f"ΔE2000={risk.get('skin_delta_e2000', 0):.1f}"
            ),
            "question": "肤色看起来偏色了吗？（只看色相，不看明暗）",
        })

    # Highlight-limited shots come from the recipe's exposure diagnostics.
    for shot_id, shot in shots.items():
        diagnostics = (shot.get("base_correction") or {}).get("exposure_diagnostics") or {}
        limited = diagnostics.get("highlight_limited")
        if not limited:
            # Reports written before exposure diagnostics were persisted: infer
            # it from the rationale text rather than losing the shot entirely.
            rationales = " ".join((shot.get("base_correction") or {}).get("rationales", []))
            limited = "headroom" in rationales
            if not limited:
                continue
        requested = diagnostics.get("requested_ev")
        applied = diagnostics.get("applied_ev", (shot.get("base_correction") or {}).get("exposure"))
        headroom = diagnostics.get("headroom_ev")
        entry = flagged.setdefault(shot_id, _blank(shot_id, {}, shots, scenes))
        entry["reasons"].append({
            "kind": "highlight_limited",
            "headline": (
                f"曝光被高光余量限制"
                + (f"：需要 {requested:+.2f} EV，只做了 {applied:+.2f} EV" if requested is not None else "")
            ),
            "detail": (
                f"高光余量 {headroom:+.2f} EV" if headroom is not None else ""
            ) + (f"  锚点={diagnostics.get('anchor', '-')}" if diagnostics else ""),
            "question": "主体是否还是太暗？压低高光换取主体更亮，值得吗？",
        })

    ordered = sorted(flagged.values(), key=lambda item: item["start_time"])
    return ordered, quality


def _blank(shot_id: str, risk: dict, shots: dict, scenes: dict) -> dict:
    shot = shots.get(shot_id, {})
    scene = scenes.get(shot.get("scene_id", ""), {})
    start = float(shot.get("start_time", risk.get("start_time", 0.0)))
    end = float(shot.get("end_time", risk.get("end_time", start)))
    return {
        "shot_id": shot_id,
        "scene_group_id": risk.get("scene_group_id", ""),
        "start_time": start,
        "end_time": end,
        "timecode": timecode(start),
        "look_strength": shot.get("look_strength"),
        "exposure": (shot.get("base_correction") or {}).get("exposure"),
        "source_cct": ((scene.get("analysis") or {}).get("white_balance") or {}).get("source_cct_kelvin"),
        "reasons": [],
    }


def resolve_source(recorded: str, root: Path) -> str:
    """Find the source media even if the recorded absolute path no longer works.

    Jobs store the path they were given. A drive letter change, a moved data
    directory, or reading the artifacts on another machine all break it, and the
    file itself is almost always still sitting in ``uploads`` under its content
    hash. Falling back to the basename costs nothing and avoids a dead end.
    """
    if recorded and Path(recorded).is_file():
        return recorded
    if not recorded:
        return ""
    candidate = root / "uploads" / Path(recorded.replace("\\", "/")).name
    if candidate.is_file():
        print(f"  note: recorded source path is unusable here, using {candidate}")
        return str(candidate)
    return ""


def side_by_side(source: np.ndarray, graded: np.ndarray, width: int) -> str:
    """One JPEG containing source and graded, labelled, as a data URI."""
    if source.shape != graded.shape:
        graded = cv2.resize(graded, (source.shape[1], source.shape[0]), interpolation=cv2.INTER_AREA)
    height = max(1, int(round(source.shape[0] * width / source.shape[1])))
    left = cv2.resize(source, (width, height), interpolation=cv2.INTER_AREA)
    right = cv2.resize(graded, (width, height), interpolation=cv2.INTER_AREA)
    gap = np.full((height, 4, 3), 32, dtype=np.uint8)
    canvas = np.hstack([left, gap, right])
    banner = np.full((26, canvas.shape[1], 3), 24, dtype=np.uint8)
    cv2.putText(banner, "SOURCE", (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (210, 210, 210), 1, cv2.LINE_AA)
    cv2.putText(banner, "GRADED", (width + 14, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (210, 210, 210), 1, cv2.LINE_AA)
    stacked = np.vstack([banner, canvas])
    ok, buffer = cv2.imencode(".jpg", stacked, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def build_html(entries: list[dict], meta: dict) -> str:
    rows = []
    for index, entry in enumerate(entries):
        kinds = sorted({reason["kind"] for reason in entry["reasons"]})
        reason_html = "".join(
            f"<div class='reason {html.escape(reason['kind'])}'>"
            f"<b>{html.escape(reason['headline'])}</b>"
            f"<span class='detail'>{html.escape(reason['detail'])}</span>"
            f"<span class='question'>{html.escape(reason['question'])}</span></div>"
            for reason in entry["reasons"]
        )
        facts = " · ".join(
            part for part in [
                f"曝光 {entry['exposure']:+.2f} EV" if entry.get("exposure") is not None else "",
                f"Look 强度 {entry['look_strength']:.2f}" if entry.get("look_strength") is not None else "",
                f"源片 {entry['source_cct']:.0f}K" if entry.get("source_cct") else "",
                html.escape(entry.get("scene_group_id") or ""),
            ] if part
        )
        rows.append(f"""
<section class="shot" data-kinds="{' '.join(kinds)}" id="s{index}">
  <header>
    <span class="tc">{html.escape(entry['timecode'])}</span>
    <span class="sid">{html.escape(entry['shot_id'])}</span>
    <span class="facts">{facts}</span>
  </header>
  {reason_html}
  <img loading="lazy" src="{entry.get('image', '')}" alt="{html.escape(entry['shot_id'])}">
  <div class="verdict" data-shot="{html.escape(entry['shot_id'])}" data-tc="{html.escape(entry['timecode'])}">
    <label><input type="radio" name="v{index}" value="ok"> 可接受</label>
    <label><input type="radio" name="v{index}" value="fix"> 需要修</label>
    <label><input type="radio" name="v{index}" value="unsure"> 不确定</label>
    <input class="note" type="text" placeholder="备注（可选）">
  </div>
</section>""")

    counts = {"large_exposure": 0, "highlight_limited": 0, "skin_hue_shift": 0, "skin_clipped": 0}
    for entry in entries:
        for reason in entry["reasons"]:
            counts[reason["kind"]] = counts.get(reason["kind"], 0) + 1

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>调色人工复核清单 · {html.escape(meta.get('job_id', ''))}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin:0; padding:24px; background:#111417; color:#e6e6e6;
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.sub {{ color:#8b949e; margin-bottom:18px; }}
.bar {{ position:sticky; top:0; background:#111417; padding:12px 0 14px;
        border-bottom:1px solid #262c33; margin-bottom:20px; z-index:5; }}
button {{ background:#1c2128; color:#e6e6e6; border:1px solid #333b44; border-radius:6px;
          padding:6px 14px; cursor:pointer; font-size:13px; margin-right:8px; }}
button.on {{ background:#2d6cdf; border-color:#2d6cdf; }}
button.export {{ float:right; background:#1f6f43; border-color:#1f6f43; }}
.shot {{ border:1px solid #262c33; border-radius:10px; padding:14px; margin-bottom:20px;
         background:#161a1e; }}
.shot header {{ display:flex; gap:14px; align-items:baseline; flex-wrap:wrap; margin-bottom:8px; }}
.tc {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:16px; color:#facc15; }}
.sid {{ color:#8b949e; font-family:ui-monospace,monospace; }}
.facts {{ color:#6e7681; font-size:12px; }}
.reason {{ padding:8px 10px; border-radius:6px; margin-bottom:8px; border-left:3px solid; }}
.reason.skin_hue_shift {{ background:#2a1d24; border-color:#d9737f; }}
.reason.skin_clipped {{ background:#2d1a1a; border-color:#e05252; }}
.reason.highlight_limited {{ background:#2a251a; border-color:#e0b341; }}
.reason.large_exposure {{ background:#1b2530; border-color:#4a9eff; }}
.reason b {{ display:block; }}
.detail {{ display:block; color:#8b949e; font-size:12px;
           font-family:ui-monospace,Menlo,Consolas,monospace; }}
.question {{ display:block; color:#c9d1d9; font-size:13px; margin-top:4px; }}
img {{ width:100%; border-radius:6px; display:block; background:#000; }}
.verdict {{ margin-top:10px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; }}
.verdict label {{ cursor:pointer; }}
.note {{ flex:1; min-width:200px; background:#0d1117; border:1px solid #333b44;
         border-radius:6px; color:#e6e6e6; padding:5px 8px; }}
.warn {{ background:#2a1d1d; border:1px solid #6b3535; border-radius:8px;
         padding:10px 14px; margin-bottom:18px; color:#ffb4b4; }}
</style></head><body>
<h1>调色人工复核清单</h1>
<div class="sub">
  任务 {html.escape(meta.get('job_id', ''))} ·
  共 {len(entries)} 个镜头需要人工判断 ·
  大幅曝光 {counts.get('large_exposure', 0)} ·
  高光受限 {counts.get('highlight_limited', 0)} ·
  肤色色相 {counts.get('skin_hue_shift', 0)} ·
  肤色死白 {counts.get('skin_clipped', 0)} ·
  全片 {meta.get('shot_count', '?')} 镜头
</div>
<div class="warn">
  评价只保存在这个页面里。看完请点「导出评价 CSV」，直接关闭会丢失。
</div>
<div class="bar">
  <button class="on" data-filter="all">全部</button>
  <button data-filter="large_exposure">只看大幅曝光</button>
  <button data-filter="highlight_limited">只看高光受限</button>
  <button data-filter="skin_hue_shift">只看肤色色相</button>
  <button data-filter="skin_clipped">只看肤色死白</button>
  <button data-filter="undone">只看未评价</button>
  <button class="export" onclick="exportCsv()">导出评价 CSV</button>
</div>
{''.join(rows)}
<script>
document.querySelectorAll('.bar button[data-filter]').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.bar button[data-filter]').forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  const f = b.dataset.filter;
  document.querySelectorAll('.shot').forEach(s => {{
    const done = !!s.querySelector('input[type=radio]:checked');
    s.style.display =
      f === 'all' ? '' :
      f === 'undone' ? (done ? 'none' : '') :
      (s.dataset.kinds.split(' ').includes(f) ? '' : 'none');
  }});
}});
function exportCsv() {{
  const rows = [['timecode','shot_id','kinds','verdict','note']];
  document.querySelectorAll('.shot').forEach(s => {{
    const v = s.querySelector('input[type=radio]:checked');
    const box = s.querySelector('.verdict');
    rows.push([box.dataset.tc, box.dataset.shot, s.dataset.kinds,
               v ? v.value : '', (s.querySelector('.note').value || '').replace(/"/g,'""')]);
  }});
  const csv = '\\uFEFF' + rows.map(r => r.map(c => '"' + c + '"').join(',')).join('\\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {{type:'text/csv'}}));
  a.download = 'review_{html.escape(meta.get("job_id", "sheet"))}.csv';
  a.click();
}}
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the manual review sheet")
    parser.add_argument("--render-job", required=True, help="project_render job id")
    parser.add_argument("--output", default="", help="output HTML path")
    parser.add_argument("--width", type=int, default=520, help="width of each panel in pixels")
    parser.add_argument("--max-shots", type=int, default=0, help="0 = no limit")
    args = parser.parse_args()

    root = data_dir()
    job_dir = root / "jobs" / args.render_job
    if not (job_dir / "project_quality_report.json").is_file():
        sys.exit(f"No QC report in {job_dir}")

    entries, quality = collect_flagged(job_dir, root)
    if args.max_shots:
        entries = entries[: args.max_shots]
    if not entries:
        sys.exit("Nothing flagged as highlight_limited or skin_hue_shift - nothing to review.")

    request = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    source_path = request.get("input_path") or ""
    graded = next(
        (item for item in job_dir.glob("*.mp4") if item.name.startswith("project_")), None
    ) or next(iter(job_dir.glob("*.mov")), None)
    if not graded:
        sys.exit(f"No rendered output found in {job_dir}")
    source_path = resolve_source(source_path, root)
    if not source_path:
        sys.exit(f"Source media not found: {request.get('input_path')}")

    print(f"{len(entries)} shot(s) to review")
    print(f"  source: {source_path}")
    print(f"  graded: {graded}")

    # Sample the middle of each flagged shot from both files at the same time.
    times = [
        min(entry["end_time"] - 0.05, entry["start_time"] + (entry["end_time"] - entry["start_time"]) / 2)
        for entry in entries
    ]
    source_frames = sample_frames_at(str(source_path), times, target_height=540)
    graded_frames = sample_frames_at(str(graded), times, target_height=540)
    for entry, left, right in zip(entries, source_frames, graded_frames):
        entry["image"] = side_by_side(left, right, args.width)
        print(f"  {entry['timecode']}  {entry['shot_id']}  "
              f"{','.join(sorted({r['kind'] for r in entry['reasons']}))}")

    meta = {
        "job_id": args.render_job,
        "shot_count": quality.get("shot_count"),
    }
    output = Path(args.output) if args.output else job_dir / "review_sheet.html"
    output.write_text(build_html(entries, meta), encoding="utf-8")
    size_mb = output.stat().st_size / 1e6
    print(f"\nWrote {output}  ({size_mb:.1f} MB, self-contained)")


if __name__ == "__main__":
    main()
