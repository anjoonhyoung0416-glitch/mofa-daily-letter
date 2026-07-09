import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urljoin, urlparse
import os
import csv
import random
import shutil
import time
import textwrap
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from google import genai

# -----------------------------
# 기본 설정
# -----------------------------
load_dotenv()

BASE = Path(__file__).parent
DATA = BASE / "data"
OUT = BASE / "output"
CARDS = OUT / "cards"

OUT.mkdir(exist_ok=True)
CARDS.mkdir(exist_ok=True)

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY가 없습니다. .env 파일을 확인하세요.")

client = genai.Client(api_key=API_KEY)

LEVEL = "중학생"   # 초등학생 / 중학생 / 고등학생 중 하나로 바꿔도 됨

LEVEL_FILE_NAMES = {
    "초등학생": "elementary.png",
    "중학생": "middle.png",
    "고등학생": "high.png",
}



# -----------------------------
# 데이터 읽기
# -----------------------------
def collect_items_from_csv():
    csv_path = DATA / "source_items_mock.csv"

    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} 파일이 없습니다.")

    items = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items.append({
                "title": row.get("title", "").strip(),
                "source": row.get("source", "").strip(),
                "link": row.get("link", "").strip(),
                "date": row.get("date", "").strip(),
                "body": row.get("body", "").strip(),
            })

    if not items:
        raise ValueError("source_items_mock.csv에 데이터가 없습니다.")

    return items

MOFA_BRIEFING_LIST_URL = "https://www.mofa.go.kr/www/brd/m_4078/list.do"


def clean_text(text):
    return " ".join(text.split())


def build_briefing_id(url: str, date: str = "") -> str:
    """외교부 상세 URL의 seq와 작성일로 안정적인 briefing_id를 만듭니다."""
    query = parse_qs(urlparse(url).query)
    seq = (query.get("seq") or [""])[0].strip()

    if not seq:
        raise ValueError(f"외교부 상세 URL에서 seq를 찾지 못했습니다: {url}")

    date_digits = re.sub(r"\D", "", date)
    if len(date_digits) == 8:
        return f"BRIEF_{date_digits}_{seq}"

    return f"BRIEF_{seq}"


def fetch_mofa_detail(url, fallback_title="외교부 정례브리핑"):
    headers = {
        "User-Agent": "Mozilla/5.0 (MOFA Daily Letter MVP; educational project)"
    }

    res = requests.get(url, headers=headers, timeout=10)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")

    lines = [
        line.strip()
        for line in soup.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]

    # 제목 찾기
    title = fallback_title
    for line in lines:
        if "정례브리핑" in line and len(line) < 80:
            title = clean_text(line)
            break

    # 작성일 찾기
    date = ""
    for i, line in enumerate(lines):
        if line == "작성일" and i + 1 < len(lines):
            date = clean_text(lines[i + 1])
            break

    if not date:
        joined = "\n".join(lines)
        m = re.search(r"\d{4}-\d{2}-\d{2}", joined)
        if m:
            date = m.group(0)

    # 본문 시작점 찾기
    start_idx = None
    for i, line in enumerate(lines):
        if line.startswith("I. 모두 발언") or line.startswith("Ⅰ. 모두 발언"):
            start_idx = i
            break

    if start_idx is None:
        for i, line in enumerate(lines):
            if "모두 발언" in line:
                start_idx = i
                break

    if start_idx is None:
        start_idx = 0

    # 본문 끝점 찾기
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i] in ["목록", "이전글", "다음글"]:
            end_idx = i
            break

    body = "\n".join(lines[start_idx:end_idx])
    body = body[:5000]  # 너무 길면 Gemini 입력이 커지므로 MVP에서는 일부만 사용

    briefing_id = build_briefing_id(url, date)

    return {
        "briefing_id": briefing_id,
        "title": title,
        "source": "외교부 브리핑",
        "link": url,
        "date": date,
        "body": body,
    }


def collect_items_from_mofa(limit=1):
    headers = {
        "User-Agent": "Mozilla/5.0 (MOFA Daily Letter MVP; educational project)"
    }

    res = requests.get(MOFA_BRIEFING_LIST_URL, headers=headers, timeout=10)
    res.raise_for_status()
    res.encoding = res.apparent_encoding

    soup = BeautifulSoup(res.text, "html.parser")

    items = []

    for a in soup.select("a[href*='view.do']"):
        title = clean_text(a.get_text(" ", strip=True))
        href = a.get("href")

        if not title:
            continue

        if "정례브리핑" not in title:
            continue

        link = urljoin(MOFA_BRIEFING_LIST_URL, href)

        print("외교부 실제 데이터 발견:", title)
        print("링크:", link)

        detail_item = fetch_mofa_detail(link, fallback_title=title)
        items.append(detail_item)

        if len(items) >= limit:
            break

    if not items:
        raise ValueError("외교부 정례브리핑 데이터를 찾지 못했습니다.")

    return items

# -----------------------------
# Gemini 요약
# -----------------------------
def summarize_with_gemini(item, level):
    prompt = f"""
너는 외교부 정례브리핑을 학생용 카드뉴스로 바꾸는 편집자다.

아래 내용을 {level} 눈높이에 맞게 4칸짜리 만화형 카드뉴스 문구로 바꿔라.

[원문 정보]
제목: {item['title']}
출처: {item['source']}
날짜: {item['date']}
본문: {item['body'][:2500]}

[작성 규칙]
- 원문을 길게 베끼지 말고 핵심만 쉽게 풀어라.
- 브리핑 형식 문구는 쓰지 마라.
- 'I. 모두 발언', '질의응답', '안녕하십니까' 같은 말은 쓰지 마라.
- 제목은 12자 이내로 써라.
- 카드1, 카드2, 카드3의 내용은 80~120자 정도로 써라.
- 카드1, 카드2, 카드3의 내용은 반드시 1~2개의 완성된 문장으로 써라.
- 카드4의 내용은 35자 이내의 한 문장으로 써라.
- 카드4는 오늘의 핵심 메시지만 써라.
- 별표, 굵게 표시, 마크다운, 번호 목록, 따옴표 장식은 절대 쓰지 마라.

[출력 형식]
반드시 아래 JSON 형식만 출력하라.
설명 문장, 코드블록, ```json 같은 표시는 절대 쓰지 마라.

[
  {{
    "title": "짧은 제목",
    "body": "쉬운 설명 문장"
  }},
  {{
    "title": "짧은 제목",
    "body": "쉬운 설명 문장"
  }},
  {{
    "title": "짧은 제목",
    "body": "쉬운 설명 문장"
  }},
  {{
    "title": "핵심 한 줄",
    "body": "35자 이내 핵심 문장"
  }}
]
"""

    max_attempts = 5
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )

            text = (response.text or "").strip()
            if not text:
                raise ValueError("Gemini 응답 본문이 비어 있습니다.")

            # 혹시 코드블록으로 감싸서 반환한 경우 제거
            text = text.replace("```json", "").replace("```", "").strip()
            data = json.loads(text)

            if not isinstance(data, list) or len(data) != 4:
                raise ValueError("Gemini JSON은 정확히 4개의 카드 배열이어야 합니다.")

            cards = []
            for index, obj in enumerate(data, start=1):
                if not isinstance(obj, dict):
                    raise ValueError(f"카드 {index}가 JSON 객체가 아닙니다.")

                title = str(obj.get("title", "")).strip()
                body = str(obj.get("body", "")).strip()

                title = title.replace("*", "").replace("#", "").strip()
                body = body.replace("*", "").replace("#", "").strip()

                if not title or not body:
                    raise ValueError(f"카드 {index}의 title 또는 body가 비어 있습니다.")

                if len(title) > 12:
                    title = title[:12] + "…"

                cards.append(f"{title} | {body}")

            return cards

        except Exception as exc:
            last_error = exc
            print(f"Gemini 요청/검증 실패 {attempt}/{max_attempts}: {exc}")

            if attempt < max_attempts:
                # 2초 → 4초 → 8초 → 16초 + 작은 랜덤 지연
                delay = (2 ** attempt) + random.uniform(0, 1)
                print(f"{delay:.1f}초 후 재시도합니다.")
                time.sleep(delay)

    raise RuntimeError(
        f"Gemini 생성이 {max_attempts}회 모두 실패했습니다. "
        "안전상 기본 문구로 대체하지 않고 전체 생성을 중단합니다."
    ) from last_error

# -----------------------------
# 폰트 로드
# -----------------------------
def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf",
        "/System/Library/Fonts/AppleGothic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_text(text, width=18):
    lines = []
    for paragraph in text.split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) or [""]
        lines.extend(wrapped)
    return "\n".join(lines)


# -----------------------------
# 카드 1장 생성
# -----------------------------
def make_card(level, item, card_text, card_no):
    img = Image.new("RGB", (1080, 1080), "#EAF3FF")
    draw = ImageDraw.Draw(img)

    title_font = load_font(48, bold=True)
    body_font = load_font(42)
    small_font = load_font(24)

    draw.rounded_rectangle((60, 60, 1020, 1020), radius=42, fill="#FFFFFF")

    draw.text(
        (100, 110),
        f"MOFA Daily Letter | {level}",
        fill="#1B365D",
        font=title_font
    )

    draw.text(
        (100, 220),
        f"[카드 {card_no}]",
        fill="#3A5A78",
        font=load_font(30, bold=True)
    )

    draw.text(
        (100, 300),
        wrap_text(card_text, 18),
        fill="#2C3E50",
        font=body_font
    )

    source_text = f"출처: {item['source']} / 수집일: {datetime.now().strftime('%Y-%m-%d')} / 시연용 MVP"
    draw.text(
        (100, 950),
        wrap_text(source_text, 42),
        fill="#5C768D",
        font=small_font
    )

    path = CARDS / f"card_{level}_{card_no}.png"
    img.save(path)
    return path

def make_summary_poster(level, item, card_texts, output_path=None):
    """
    일간지 + 만화풍 4분할 종합 카드뉴스 생성
    결과물: output/summary_중학생.png
    """

    W, H = 1600, 1600
    img = Image.new("RGB", (W, H), "#FFF4DF")
    draw = ImageDraw.Draw(img)

    # 폰트
    title_font = load_font(82, bold=True)
    sub_font = load_font(38, bold=True)
    badge_font = load_font(26, bold=True)
    panel_title_font = load_font(36, bold=True)
    body_font = load_font(26)
    small_font = load_font(22)
    core_font = load_font(30, bold=True)

    # -----------------------------
    # 내부 유틸
    # -----------------------------
    def text_width(text, font):
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    def wrap_by_pixel(text, font, max_width):
        lines = []
        text = str(text).replace("\r", "").strip()

        for paragraph in text.split("\n"):
            current = ""

            for ch in paragraph:
                test = current + ch
                if text_width(test, font) <= max_width:
                    current = test
                else:
                    if current:
                        lines.append(current)
                    current = ch

            if current:
                lines.append(current)

        return lines

    def draw_text_box(text, box, font, fill="#222222", spacing=8):
        x1, y1, x2, y2 = box
        max_width = x2 - x1
        max_height = y2 - y1

        lines = wrap_by_pixel(text, font, max_width)
        line_h = font.getbbox("가")[3] - font.getbbox("가")[1] + spacing
        max_lines = max(1, max_height // line_h)

        if len(lines) > max_lines:
            lines = lines[:max_lines]

        y = y1
        for line in lines:
            draw.text((x1, y), line, font=font, fill=fill)
            y += line_h

    def split_card(raw, default_title):
        raw = str(raw).strip()

        if "|" in raw:
            title, body = raw.split("|", 1)
            title = title.strip()
            body = body.strip()
        else:
            title = default_title
            body = raw

        # 제목이 너무 길면 자르기
        if len(title) > 12:
            title = title[:12] + "…"

        return title, body

    def draw_simple_icon(kind, cx, cy, color):
        """
        이모지 대신 직접 그리는 간단한 만화 아이콘
        """
        if kind == "news":
            # 신문
            draw.rounded_rectangle((cx - 55, cy - 45, cx + 55, cy + 45), radius=10, fill="#FFFFFF", outline=color, width=4)
            draw.rectangle((cx - 40, cy - 25, cx + 35, cy - 15), fill=color)
            draw.line((cx - 40, cy, cx + 35, cy), fill=color, width=4)
            draw.line((cx - 40, cy + 18, cx + 20, cy + 18), fill=color, width=4)

        elif kind == "globe":
            # 지구
            draw.ellipse((cx - 50, cy - 50, cx + 50, cy + 50), fill="#EAF7FF", outline=color, width=5)
            draw.arc((cx - 30, cy - 50, cx + 30, cy + 50), 90, 270, fill=color, width=3)
            draw.arc((cx - 30, cy - 50, cx + 30, cy + 50), -90, 90, fill=color, width=3)
            draw.line((cx - 45, cy, cx + 45, cy), fill=color, width=3)
            draw.arc((cx - 45, cy - 25, cx + 45, cy + 25), 0, 180, fill=color, width=3)
            draw.arc((cx - 45, cy - 25, cx + 45, cy + 25), 180, 360, fill=color, width=3)

        elif kind == "people":
            # 사람 둘
            draw.ellipse((cx - 60, cy - 35, cx - 25, cy), fill="#FFE0BD", outline=color, width=3)
            draw.ellipse((cx + 25, cy - 35, cx + 60, cy), fill="#FFE0BD", outline=color, width=3)
            draw.rounded_rectangle((cx - 75, cy + 5, cx - 10, cy + 65), radius=18, fill="#DDEBFF", outline=color, width=3)
            draw.rounded_rectangle((cx + 10, cy + 5, cx + 75, cy + 65), radius=18, fill="#FFE9CC", outline=color, width=3)
            draw.line((cx - 10, cy + 35, cx + 10, cy + 35), fill=color, width=5)

        elif kind == "idea":
            # 전구
            draw.ellipse((cx - 42, cy - 55, cx + 42, cy + 30), fill="#FFF4B8", outline=color, width=5)
            draw.rectangle((cx - 25, cy + 28, cx + 25, cy + 60), fill="#FFFFFF", outline=color, width=4)
            draw.line((cx - 18, cy + 40, cx + 18, cy + 40), fill=color, width=3)
            draw.line((cx - 18, cy + 52, cx + 18, cy + 52), fill=color, width=3)

    # -----------------------------
    # 배경 장식
    # -----------------------------
    draw.rectangle((0, 0, W, H), fill="#FFF4DF")
    draw.rounded_rectangle((40, 35, W - 40, H - 35), radius=35, outline="#F0C36A", width=6)

    # 상단 신문 느낌
    draw.text((90, 60), "MOFA DAILY LETTER", font=badge_font, fill="#D96C00")
    draw.line((90, 100, 1510, 100), fill="#E2B15F", width=4)

    draw.text(
        (90, 125),
        "외교부 정례브리핑, 쉽게 알아보기",
        fill="#0B2A5B",
        font=title_font
    )

    draw.text(
        (95, 220),
        f"{level}용 1분 외교 카드뉴스",
        fill="#E86A00",
        font=sub_font
    )

    # -----------------------------
    # 패널 설정
    # -----------------------------
    panels = [
        {
            "box": (70, 300, 760, 735),
            "label": "1",
            "default_title": "오늘의 소식",
            "color": "#5B8C2A",
            "light": "#EAF6D7",
            "icon": "news",
        },
        {
            "box": (840, 300, 1530, 735),
            "label": "2",
            "default_title": "왜 중요해?",
            "color": "#356BB3",
            "light": "#E4F0FF",
            "icon": "globe",
        },
        {
            "box": (70, 790, 760, 1225),
            "label": "3",
            "default_title": "우리와 연결",
            "color": "#6C50B8",
            "light": "#EFE8FF",
            "icon": "people",
        },
        {
            "box": (840, 790, 1530, 1225),
            "label": "4",
            "default_title": "핵심 한 줄",
            "color": "#D9822B",
            "light": "#FFEAD1",
            "icon": "idea",
        },
    ]

    parsed_cards = []
    for idx, panel in enumerate(panels):
        raw = card_texts[idx] if idx < len(card_texts) else ""
        parsed_cards.append(split_card(raw, panel["default_title"]))

    # -----------------------------
    # 패널 그리기
    # -----------------------------
    for idx, panel in enumerate(panels):
        x1, y1, x2, y2 = panel["box"]
        color = panel["color"]
        light = panel["light"]
        title, body = parsed_cards[idx]

        # 바깥 패널
        draw.rounded_rectangle(
            (x1, y1, x2, y2),
            radius=28,
            fill=light,
            outline=color,
            width=6
        )

        # 제목 바
        draw.rounded_rectangle(
            (x1 + 18, y1 + 18, x2 - 18, y1 + 88),
            radius=20,
            fill=color
        )

        draw.text(
            (x1 + 38, y1 + 33),
            f"{panel['label']}. {title}",
            fill="#FFFFFF",
            font=panel_title_font
        )

        # 아이콘 원
        
    #    icon_cx, icon_cy = x2 - 95, y1 + 162
    #    draw.ellipse(
    #        (icon_cx - 72, icon_cy - 72, icon_cx + 72, icon_cy + 72),
    #        fill="#FFFFFF",
    #        outline=color,
    #        width=5
    #    )
    #   draw_simple_icon(panel["icon"], icon_cx, icon_cy, color)
        
        # 말풍선 본문
        speech = (x1 + 38, y1 + 120, x2 - 38, y2 - 45)
        draw.rounded_rectangle(
            speech,
            radius=26,
            fill="#FFFFFF",
            outline="#C9C9C9",
            width=3
        )

        # 말풍선 꼬리
        sx1, sy1, sx2, sy2 = speech
        tail = [
            (sx2 - 40, sy2 - 35),
            (sx2 - 5, sy2 - 15),
            (sx2 - 40, sy2 - 5),
        ]
        draw.polygon(tail, fill="#FFFFFF", outline="#C9C9C9")

        draw_text_box(
            body,
            (sx1 + 28, sy1 + 28, sx2 - 28, sy2 - 28),
            body_font,
            fill="#263447",
            spacing=9
        )

    # -----------------------------
    # 하단 핵심 박스
    # -----------------------------
    core_title, core_body = parsed_cards[3]
    core_text = core_body.replace("\n", " ")
    if len(core_text) > 45:
        core_text = core_text[:45] + "…"

    draw.rounded_rectangle(
        (80, 1280, 1520, 1405),
        radius=34,
        fill="#FFFFFF",
        outline="#E4A23A",
        width=6
    )

    draw_text_box(
        f"오늘의 핵심: {core_text}",
        (120, 1317, 1480, 1380),
        core_font,
        fill="#0B2A5B",
        spacing=8
    )

    # 출처
    source_text = (
        f"출처: {item['source']} / 원문일: {item['date']} / "
        f"수집일: {datetime.now().strftime('%Y-%m-%d')} / 시연용 MVP"
    )

    draw_text_box(
        source_text,
        (95, 1440, 1505, 1495),
        small_font,
        fill="#6E7B8B",
        spacing=4
    )

    path = Path(output_path) if output_path else OUT / f"summary_{level}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path
# -----------------------------
# HTML 미리보기 생성
# -----------------------------
def build_newsletter_html(level, item, card_paths):
    image_blocks = ""
    for p in card_paths:
        image_blocks += f"""
        <div style="margin-bottom:24px;">
            <p><b>{p.name}</b></p>
            <img src="cards/{p.name}" style="max-width:360px; border:1px solid #ddd;">
        </div>
        """

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>MOFA Daily Letter Preview</title>
    </head>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; padding: 30px;">
        <h2>MOFA Daily Letter - {level}용 쉬운 외교 소식</h2>
        <p><b>오늘의 자료:</b> {item['title']}</p>
        <p><b>출처:</b> {item['source']} / <a href="{item['link']}">원문 보기</a></p>
        <p>아래 카드뉴스 이미지는 Python + Gemini API로 자동 생성한 시연용 결과물입니다.</p>
        <hr>
        {image_blocks}
        <hr>
        <p style="font-size:12px; color:#666;">
            AI 생성 문안은 PM 검수 후 활용합니다.
            구독 해지는 회신으로 요청할 수 있습니다.
        </p>
    </body>
    </html>
    """

    preview_path = OUT / "newsletter_preview.html"
    preview_path.write_text(html, encoding="utf-8")
    return preview_path


# -----------------------------
# 안전한 브리핑별 생성 파이프라인
# -----------------------------
def validate_generated_files(directory: Path) -> dict[str, Path]:
    expected = {
        level: directory / filename
        for level, filename in LEVEL_FILE_NAMES.items()
    }

    missing = [str(path) for path in expected.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "수준별 카드뉴스 생성 검증 실패. 누락 파일: " + ", ".join(missing)
        )

    empty = [str(path) for path in expected.values() if path.stat().st_size == 0]
    if empty:
        raise ValueError(
            "수준별 카드뉴스 파일 크기가 0입니다: " + ", ".join(empty)
        )

    return expected


def generate_latest_briefing() -> dict:
    """최신 정례브리핑 1건을 수집하고 3개 수준 카드뉴스를 원자적으로 생성합니다."""
    items = collect_items_from_mofa(limit=1)
    item = items[0]
    briefing_id = item["briefing_id"]

    final_dir = CARDS / briefing_id
    staging_dir = CARDS / f".{briefing_id}.tmp"

    if final_dir.exists():
        try:
            files = validate_generated_files(final_dir)
            print(f"이미 완성된 카드뉴스 폴더가 있습니다: {final_dir}")
            return {
                "briefing_id": briefing_id,
                "title": item["title"],
                "date": item["date"],
                "link": item["link"],
                "cards_dir": str(final_dir),
                "files": {level: str(path) for level, path in files.items()},
                "reused": True,
            }
        except Exception:
            print(f"불완전한 기존 폴더를 제거하고 다시 생성합니다: {final_dir}")
            shutil.rmtree(final_dir)

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        generated: dict[str, Path] = {}

        for level, filename in LEVEL_FILE_NAMES.items():
            print(f"\n[{level}] Gemini 요약 생성 중...")
            card_texts = summarize_with_gemini(item, level)

            output_path = staging_dir / filename
            print(f"[{level}] 종합 카드뉴스 생성 중...")
            generated[level] = make_summary_poster(
                level=level,
                item=item,
                card_texts=card_texts,
                output_path=output_path,
            )
            print(f"[{level}] 완료: {generated[level]}")

        validate_generated_files(staging_dir)

        metadata = {
            "briefing_id": briefing_id,
            "title": item["title"],
            "date": item["date"],
            "link": item["link"],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "files": {
                level: LEVEL_FILE_NAMES[level]
                for level in LEVEL_FILE_NAMES
            },
        }
        (staging_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        staging_dir.replace(final_dir)
        files = validate_generated_files(final_dir)

        return {
            **metadata,
            "cards_dir": str(final_dir),
            "files": {level: str(path) for level, path in files.items()},
            "reused": False,
        }

    except Exception:
        # 하나라도 실패하면 임시 폴더를 삭제하여 이전/불완전 카드를 발송 후보로 남기지 않습니다.
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise


def main():
    result = generate_latest_briefing()

    print("\n=== 생성 완료 ===")
    print("브리핑 ID:", result["briefing_id"])
    print("제목:", result["title"])
    print("작성일:", result["date"])
    print("카드 폴더:", result["cards_dir"])
    print(json.dumps(result["files"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
