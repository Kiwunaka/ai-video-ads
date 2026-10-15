"""Mix timed audio from media files without silently dropping source sound."""

import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile


SAMPLE_RATE = 48000


def number(value, name, minimum=0):
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return result


def audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=duration:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    info = json.loads(result.stdout)
    if not info.get("streams"):
        raise ValueError(f"Source has no audio stream: {path}")
    value = info["streams"][0].get("duration")
    if value in (None, "N/A"):
        value = info.get("format", {}).get("duration")
    if value in (None, "N/A"):
        raise ValueError(f"Audio duration unavailable: {path}")
    return number(value, "source duration")


def mix(manifest, output):
    root = manifest.resolve().parent
    spec = json.loads(manifest.read_text(encoding="utf-8-sig"))
    total = number(spec["duration"], "duration", 0.00001)
    clips = spec["clips"]
    if not clips:
        raise ValueError("The mix must contain at least one audio clip")
    command = ["ffmpeg", "-v", "error", "-y"]
    filters = []
    for index, clip in enumerate(clips):
        source = (root / clip["src"]).resolve()
        start = number(clip["start"], "start")
        source_start = number(clip.get("source_start", 0), "source_start")
        length = number(clip["duration"], "clip duration", 0.00001)
        gain = number(clip.get("gain", 1), "gain")
        fade_in = number(clip.get("fade_in", 0), "fade_in")
        fade_out = number(clip.get("fade_out", 0), "fade_out")
        if start + length > total + 1 / SAMPLE_RATE:
            raise ValueError(f"Clip {index + 1} extends beyond the edit duration")
        if source_start + length > audio_duration(source) + 1 / SAMPLE_RATE:
            raise ValueError(f"Clip {index + 1} extends beyond the source audio")
        if max(fade_in, fade_out) > length:
            raise ValueError(f"Clip {index + 1}: fade is longer than the clip")
        command += ["-i", str(source)]
        chain = (
            f"[{index}:a:0]atrim=start={source_start:.9f}:duration={length:.9f},"
            f"asetpts=PTS-STARTPTS,aresample={SAMPLE_RATE},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={gain:.9f}"
        )
        if fade_in:
            chain += f",afade=t=in:st=0:d={fade_in:.9f}"
        if fade_out:
            chain += f",afade=t=out:st={length-fade_out:.9f}:d={fade_out:.9f}"
        chain += f",adelay={round(start*SAMPLE_RATE)}S:all=1[a{index}]"
        filters.append(chain)
    inputs = "".join(f"[a{i}]" for i in range(len(clips)))
    filters.append(
        inputs + f"amix=inputs={len(clips)}:duration=longest:"
        "dropout_transition=0:normalize=0,"
        "alimiter=limit=0.89:level=false:latency=true,"
        f"apad,atrim=duration={total:.9f},asetpts=PTS-STARTPTS[mix]"
    )
    output = output.resolve()
    if output in {(root / clip["src"]).resolve() for clip in clips}:
        raise ValueError("The output must not replace one of its source files")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=output.stem + "-", suffix=".wav", dir=output.parent, delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        command += ["-filter_complex", ";".join(filters), "-map", "[mix]", "-vn",
                    "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s24le",
                    str(temporary)]
        subprocess.run(command, check=True, capture_output=True)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(json.dumps({"output": str(output), "duration": total,
                      "sample_rate": SAMPLE_RATE, "channels": 2}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        mix(args.manifest, args.output)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            detail = error.stderr
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            parser.exit(1, detail.strip() + "\n")
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
