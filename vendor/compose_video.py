#!/usr/bin/env python3
"""按分镜音频真实时长合成静态图视频；默认只读检查，--render 才编码。"""

import argparse
import json
import math
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


class VideoError(Exception):
    pass


def run(command, timeout=None):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise VideoError(f"{Path(command[0]).name} 失败：{result.stderr[-2000:]}")
    return result.stdout


def probe(path, ffprobe):
    return json.loads(run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)
    ], timeout=60))


def duration(metadata):
    value = float(metadata.get("format", {}).get("duration", "nan"))
    if not math.isfinite(value) or value <= 0:
        raise VideoError("媒体缺少有效的正数时长")
    return value


def stream(metadata, kind):
    found = next((s for s in metadata.get("streams", []) if s.get("codec_type") == kind), None)
    if found is None:
        raise VideoError(f"媒体缺少 {kind} 流")
    return found


def media_path(value, base):
    if not isinstance(value, str) or not value.strip():
        raise VideoError("image/audio 必须是非空本地路径")
    path = (base / Path(value).expanduser()).resolve()
    if not path.is_file():
        raise VideoError(f"素材不存在：{path}")
    return path


def load_scenes(manifest, ffprobe):
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise VideoError("清单必须是 schema_version=1 的 JSON 对象")
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise VideoError("scenes 必须是非空数组")
    items, seen = [], set()
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise VideoError(f"第 {index} 镜不是对象")
        scene_id = scene.get("id")
        if not isinstance(scene_id, str) or not scene_id.strip() or scene_id in seen:
            raise VideoError(f"第 {index} 镜 id 为空或重复")
        seen.add(scene_id)
        image = media_path(scene.get("image"), manifest.parent)
        audio = media_path(scene.get("audio"), manifest.parent)
        if image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            raise VideoError(f"不支持的静态图片格式：{image}")
        image_stream = stream(probe(image, ffprobe), "video")
        if not image_stream.get("width") or not image_stream.get("height"):
            raise VideoError(f"图片尺寸无效：{image}")
        audio_metadata = probe(audio, ffprobe)
        stream(audio_metadata, "audio")
        items.append({
            "id": scene_id, "image": str(image), "audio": str(audio),
            "audio_duration_seconds": duration(audio_metadata),
            "clip_name": f"clip-{index:04d}.mp4",
        })
    return items


def verify_video(path, expected, args, ffprobe, tolerance, expected_frames=None):
    metadata = probe(path, ffprobe)
    video, audio = stream(metadata, "video"), stream(metadata, "audio")
    if (video.get("codec_name"), video.get("width"), video.get("height")) != (
        "h264", args.width, args.height
    ):
        raise VideoError(f"视频编码或尺寸不符：{path}")
    frames = int(video.get("nb_frames", 0))
    if expected_frames is None:
        if Fraction(video.get("r_frame_rate", "0/1")) != args.fps:
            raise VideoError(f"视频帧率不符：{path}")
    elif frames != expected_frames or abs(float(video["duration"]) - frames / args.fps) > tolerance:
        raise VideoError(f"拼接后的帧数或视频时间轴不符：{path}")
    if (audio.get("codec_name"), audio.get("channels"), audio.get("sample_rate")) != (
        "aac", 2, "44100"
    ):
        raise VideoError(f"音频格式不符：{path}")
    actual = duration(metadata)
    if abs(actual - expected) > tolerance:
        raise VideoError(
            f"时长偏差超限：{path.name}，预期 {expected:.6f}s，实测 {actual:.6f}s，"
            f"容差 {tolerance:.6f}s"
        )
    return {
        "duration_seconds": actual, "frame_count": frames,
        "r_frame_rate": video.get("r_frame_rate"), "avg_frame_rate": video.get("avg_frame_rate"),
    }


def clip_command(item, args, ffmpeg, work):
    size = f"{args.width}:{args.height}"
    filters = (
        f"scale={size}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={size}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,format=yuv420p"
    )
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n",
        "-loop", "1", "-framerate", str(args.fps), "-i", item["image"],
        "-i", item["audio"], "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{item['audio_duration_seconds']:.9f}", "-vf", filters,
        "-r", str(args.fps), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", str(args.crf), "-tune", "stillimage", "-c:a", "aac",
        "-b:a", "192k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart",
        str(work / item["clip_name"]),
    ]


def compose(args):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VideoError("PATH 中需要 ffmpeg 和 ffprobe；脚本不会自动安装")
    manifest = args.manifest.expanduser().resolve()
    work, output = args.work_dir.expanduser().resolve(), args.output.expanduser().resolve()
    report_path = output.with_suffix(".build.json")
    if output.suffix.lower() != ".mp4":
        raise VideoError("--output 必须是 .mp4 文件")
    if work.exists() or output.exists() or report_path.exists():
        raise VideoError("工作目录、目标视频或构建清单已存在；请使用新的路径，不覆盖旧产物")
    if output.is_relative_to(work) or work.is_relative_to(output):
        raise VideoError("工作目录和目标视频必须分开存放")
    items = load_scenes(manifest, ffprobe)
    expected = sum(item["audio_duration_seconds"] for item in items)
    # 覆盖帧舍入和 AAC 包边界；逐镜校验能拦截抵消后的局部漂移。
    clip_tolerance = 1 / args.fps + 0.05
    total_tolerance = len(items) * clip_tolerance + 0.05
    result = {
        "status": "planned", "manifest": str(manifest), "output": str(output),
        "work_dir": str(work), "scene_count": len(items),
        "width": args.width, "height": args.height, "fps": args.fps, "crf": args.crf,
        "sum_audio_duration_seconds": expected,
        "clip_tolerance_seconds": clip_tolerance, "total_tolerance_seconds": total_tolerance,
        "items": items,
    }
    if not args.render:
        return result

    work.mkdir(parents=True, exist_ok=False)
    for item in items:
        print(f"合成 {item['id']}…", file=sys.stderr, flush=True)
        run(clip_command(item, args, ffmpeg, work))
        checked = verify_video(
            work / item["clip_name"], item["audio_duration_seconds"], args, ffprobe, clip_tolerance
        )
        actual = checked["duration_seconds"]
        item["frame_count"] = checked["frame_count"]
        item["clip_duration_seconds"] = actual
        item["delta_seconds"] = actual - item["audio_duration_seconds"]

    clip_list = work / "clips.txt"
    clip_list.write_text("".join(f"file '{item['clip_name']}'\n" for item in items), encoding="utf-8")
    staged = work / "assembled.mp4"
    run([
        ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-n", "-f", "concat",
        "-safe", "1", "-i", str(clip_list), "-c", "copy", "-movflags", "+faststart", str(staged),
    ])
    # AAC 与视频帧边界不总重合，stream-copy 后平均帧率可能略偏；核对总帧数与时间轴。
    checked = verify_video(
        staged, expected, args, ffprobe, total_tolerance,
        expected_frames=sum(item["frame_count"] for item in items),
    )
    actual = checked["duration_seconds"]
    result.update({
        "status": "rendered", "technical_validation": "passed",
        "visual_review": "not_run", "listening_review": "not_run",
        "output_duration_seconds": actual, "total_delta_seconds": actual - expected,
        "max_abs_clip_delta_seconds": max(abs(item["delta_seconds"]) for item in items),
        "output_frame_count": checked["frame_count"],
        "output_r_frame_rate": checked["r_frame_rate"],
        "output_avg_frame_rate": checked["avg_frame_rate"],
        "build_report": str(report_path),
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as target, staged.open("rb") as source:
        shutil.copyfileobj(source, target)
    with report_path.open("x", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
        target.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="schema_version=1 的分镜 JSON")
    parser.add_argument("--work-dir", type=Path, required=True, help="尚不存在的过程目录")
    parser.add_argument("--output", type=Path, required=True, help="尚不存在的最终 .mp4 路径")
    parser.add_argument("--render", action="store_true", help="真实编码；省略则只读检查并输出计划")
    parser.add_argument("--width", type=int, default=1920, help="输出宽度，正偶数，默认 1920")
    parser.add_argument("--height", type=int, default=1080, help="输出高度，正偶数，默认 1080")
    parser.add_argument("--fps", type=int, default=30, help="正整数帧率，默认 30")
    parser.add_argument("--crf", type=int, default=18, help="H.264 CRF，0–51，默认 18")
    args = parser.parse_args()
    if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        parser.error("宽高必须为正偶数")
    if args.fps <= 0 or not 0 <= args.crf <= 51:
        parser.error("fps 必须为正整数；crf 必须在 0–51")
    try:
        print(json.dumps(compose(args), ensure_ascii=False, indent=2))
        return 0
    except (VideoError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
