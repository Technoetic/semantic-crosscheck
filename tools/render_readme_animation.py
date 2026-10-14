"""Render the explanatory README GIF with Pillow (optional documentation tool).

Pass font files supporting Korean with --font, --font-bold and --font-mono.
No service, database or runtime configuration is accessed.
"""

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

STEPS = (
    ("demo", "가상 데이터 적재"),
    ("doctor", "환경·권한 확인"),
    ("build", "규칙·계층 폐포 생성"),
    ("reason", "SHACL·OWL 검증"),
    ("cube", "Cube 모델 생성·검사"),
    ("verify", "기준 SQL과 교차 검증"),
    ("report", "최종 판정·한계 보고"),
)
SIZE = (1080, 480)
BACKGROUND = "#101827"
INK = "#f8fafc"
MUTED = "#cbd5e1"
ACCENT = "#7dd3fc"
NOTICE = "동작 설명 애니메이션 · 실제 실행 기록은 아님"


def render(font_path, bold_path, mono_path):
    fonts = {
        "title": ImageFont.truetype(str(bold_path), 34),
        "body": ImageFont.truetype(str(font_path), 22),
        "description": ImageFont.truetype(str(bold_path), 28),
        "notice": ImageFont.truetype(str(font_path), 19),
        "label": ImageFont.truetype(str(mono_path), 22),
        "command": ImageFont.truetype(str(mono_path), 22),
    }
    frames, durations = [], []
    centers = [107 + i * 144 for i in range(7)]
    for active, (command, description) in enumerate(STEPS):
        for tick in range(8):
            img = Image.new("RGB", SIZE, BACKGROUND)
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((1, 1, 1078, 478), radius=22, outline="#334155", width=2)
            draw.rounded_rectangle((40, 29, 98, 34), radius=2, fill=ACCENT)
            draw.text((40, 47), "데모 실행 흐름", font=fonts["title"], fill=INK)
            draw.text((41, 95), "환경 확인 → 계층 생성 → 서로의 답 비교 → 결과 종합", font=fonts["body"], fill=MUTED)
            for index, (label, _) in enumerate(STEPS):
                x = centers[index] - 64
                is_active = index == active
                draw.rounded_rectangle((x, 149, x+128, 206), radius=12,
                                       fill="#12314b" if is_active else "#172237",
                                       outline=ACCENT if is_active else "#475569",
                                       width=3 if is_active else 1)
                text_width = draw.textlength(label, font=fonts["label"])
                assert text_width < 118, label
                draw.text((centers[index]-text_width/2, 165), label,
                          font=fonts["label"], fill=INK if is_active else MUTED)
            draw.line((centers[0], 239, centers[-1], 239), fill="#475569", width=3)
            # A gentle moving marker travels from the previous stage and rests on the active stage
            # during the hold, matching the highlighted tab and caption; it is not a test verdict.
            for center in centers:
                draw.ellipse((center-4, 235, center+4, 243), fill="#64748b")
            origin, destination = centers[max(active-1, 0)], centers[active]
            fraction = (1-math.cos(math.pi*tick/7))/2
            moving_x = origin+(destination-origin)*fraction
            draw.ellipse((moving_x-7, 232, moving_x+7, 246), fill=ACCENT)
            draw.rounded_rectangle((40, 272, 1040, 402), radius=14,
                                   fill="#090f1c", outline="#475569", width=1)
            draw.text((62, 287), f"{active+1:02d} / 07   {command}", font=fonts["label"], fill=ACCENT)
            draw.text((62, 320), description, font=fonts["description"], fill=INK)
            command_line = f"$ python sxc.py {command} --config demo/crosscheck.toml"
            assert draw.textlength(command_line, font=fonts["command"]) < 950
            draw.text((62, 364), command_line, font=fonts["command"], fill=MUTED)
            draw.text((41, 432), NOTICE, font=fonts["notice"], fill=MUTED)
            frames.append(img)
            durations.append(1100 if tick == 7 else 60)
    # One shared palette prevents colour drift between frames.
    palette = frames[0].quantize(colors=128)
    indexed = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    return indexed, durations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--font-bold", type=Path, required=True)
    parser.add_argument("--font-mono", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs/images/semantic-crosscheck-flow.gif")
    args = parser.parse_args()
    frames, durations = render(args.font, args.font_bold, args.font_mono)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(args.output, save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, disposal=1, optimize=True,
                   comment=NOTICE.encode("utf-8"))
    print(f"Wrote {args.output.name}: {len(frames)} frames, {sum(durations)} ms, {args.output.stat().st_size} bytes")


if __name__ == "__main__":
    main()
