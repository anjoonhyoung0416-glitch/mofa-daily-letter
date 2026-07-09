from __future__ import annotations

import argparse
import base64
import csv
import mimetypes
import os
import re
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]

PROJECT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TOKEN_FILE = PROJECT_DIR / "token_google.json"
LOG_FILE = PROJECT_DIR / "send_log.csv"

SEND_HISTORY_SHEET = "send_history"
SEND_HISTORY_RANGE = f"{SEND_HISTORY_SHEET}!A:H"
SEND_HISTORY_HEADERS = [
    "briefing_id",
    "email",
    "level",
    "newsletter",
    "status",
    "sent_at",
    "message_id",
    "detail",
]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_config() -> dict[str, str]:
    """Load configuration from .env."""
    load_dotenv(PROJECT_DIR / ".env")

    spreadsheet_id = os.getenv("SPREADSHEET_ID", "").strip()
    sheet_range = os.getenv("SHEET_RANGE", "A:Z").strip()
    output_dir = os.getenv("OUTPUT_DIR", "output").strip()
    sender_name = os.getenv("SENDER_NAME", "MOFA Daily Letter").strip()
    site_url = os.getenv(
        "SITE_URL",
        "https://anjoonhyoung0416-glitch.github.io/mofa-daily-letter/",
    ).strip()

    if not spreadsheet_id:
        raise ValueError(
            "SPREADSHEET_ID가 없습니다. .env 파일에 "
            "SPREADSHEET_ID=구글시트_ID 를 추가하세요."
        )

    return {
        "spreadsheet_id": spreadsheet_id,
        "sheet_range": sheet_range,
        "output_dir": output_dir,
        "sender_name": sender_name,
        "site_url": site_url,
    }


def get_credentials() -> Credentials:
    """
    Create or refresh OAuth credentials shared by Sheets API and Gmail API.

    기존 token_google.json의 권한 범위가 부족하면 새 OAuth 승인을 받습니다.
    """
    creds: Credentials | None = None

    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(TOKEN_FILE),
                SCOPES,
            )
        except Exception as exc:
            print(f"기존 token_google.json을 읽지 못했습니다: {exc}")
            creds = None

    if creds and not creds.has_scopes(SCOPES):
        print("기존 Google 토큰의 권한이 부족합니다. 새 인증을 진행합니다.")
        creds = None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"{CREDENTIALS_FILE.name} 파일이 없습니다. "
                "Google Cloud에서 Desktop app OAuth 클라이언트 JSON을 받아 "
                "프로젝트 루트에 credentials.json 이름으로 넣으세요."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            SCOPES,
        )
        creds = flow.run_local_server(port=0)

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def find_column(headers: list[str], keywords: list[str]) -> int | None:
    normalized = [normalize_header(h) for h in headers]
    for index, header in enumerate(normalized):
        if all(normalize_header(k) in header for k in keywords):
            return index
    return None


def cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index]).strip()


def is_consent_yes(value: str) -> bool:
    normalized = normalize_header(value)
    allowed = {
        "네",
        "예",
        "동의",
        "동의합니다",
        "yes",
        "true",
        "y",
    }
    return normalized in allowed or normalized.startswith("동의")


def ensure_send_history_sheet(
    sheets_service: Any,
    spreadsheet_id: str,
) -> None:
    """
    send_history 시트가 없으면 자동 생성하고,
    첫 행이 비어 있으면 헤더를 작성합니다.
    """
    spreadsheet = (
        sheets_service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        )
        .execute()
    )

    titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in spreadsheet.get("sheets", [])
    }

    if SEND_HISTORY_SHEET not in titles:
        print(f"'{SEND_HISTORY_SHEET}' 시트가 없어 자동 생성합니다.")
        (
            sheets_service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": SEND_HISTORY_SHEET,
                                }
                            }
                        }
                    ]
                },
            )
            .execute()
        )

    result = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=SEND_HISTORY_RANGE,
        )
        .execute()
    )

    rows = result.get("values", [])
    if rows:
        return

    (
        sheets_service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=f"{SEND_HISTORY_SHEET}!A1:H1",
            valueInputOption="RAW",
            body={"values": [SEND_HISTORY_HEADERS]},
        )
        .execute()
    )


def get_sent_keys(
    sheets_service: Any,
    spreadsheet_id: str,
) -> set[tuple[str, str]]:
    """
    send_history에서 SUCCESS 상태인
    (briefing_id, email) 조합을 읽습니다.
    """
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=SEND_HISTORY_RANGE,
        )
        .execute()
    )

    rows = result.get("values", [])
    if len(rows) < 2:
        return set()

    sent_keys: set[tuple[str, str]] = set()

    for row in rows[1:]:
        briefing_id = row[0].strip() if len(row) > 0 else ""
        email = row[1].strip().lower() if len(row) > 1 else ""
        status = row[4].strip().upper() if len(row) > 4 else ""

        if briefing_id and email and status == "SUCCESS":
            sent_keys.add((briefing_id, email))

    return sent_keys


def append_send_history(
    sheets_service: Any,
    spreadsheet_id: str,
    briefing_id: str,
    email: str,
    level: str,
    newsletter: Path,
    status: str,
    message_id: str = "",
    detail: str = "",
) -> None:
    """Append one send result row to Google Sheets."""
    values = [[
        briefing_id,
        email,
        level,
        newsletter.name,
        status,
        datetime.now().isoformat(timespec="seconds"),
        message_id,
        detail,
    ]]

    (
        sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=SEND_HISTORY_RANGE,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


def get_subscribers(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_range: str,
) -> list[dict[str, str]]:
    """Read valid subscribers from the Google Form response sheet."""
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=sheet_range,
        )
        .execute()
    )

    rows = result.get("values", [])
    if len(rows) < 2:
        return []

    headers = [str(x) for x in rows[0]]

    email_col = find_column(headers, ["이메일"])
    level_col = find_column(headers, ["학습", "수준"])
    consent_col = find_column(headers, ["개인정보", "동의"])

    if email_col is None:
        raise ValueError(
            "이메일 열을 찾지 못했습니다. "
            "Google Form 응답 시트의 헤더에 '이메일'이 포함되어 있는지 확인하세요."
        )

    if consent_col is None:
        raise ValueError(
            "개인정보 동의 열을 찾지 못했습니다. "
            "헤더에 '개인정보'와 '동의'가 포함되어 있는지 확인하세요."
        )

    # 같은 이메일이 여러 번 신청한 경우 가장 마지막 응답을 사용합니다.
    unique_by_email: dict[str, dict[str, str]] = {}

    for row in rows[1:]:
        email = cell(row, email_col).lower()
        level = cell(row, level_col) or "공통"
        consent = cell(row, consent_col)

        if not EMAIL_PATTERN.match(email):
            continue

        if not is_consent_yes(consent):
            continue

        unique_by_email[email] = {
            "email": email,
            "level": level,
            "consent": consent,
        }

    return list(unique_by_email.values())


def validate_briefing_id(briefing_id: str) -> str:
    """Validate the briefing ID before using it as a folder name."""
    value = briefing_id.strip()
    if not re.fullmatch(r"BRIEF_[A-Za-z0-9_-]+", value):
        raise ValueError(
            "briefing_id 형식이 올바르지 않습니다. "
            "예: BRIEF_20260709_368852"
        )
    return value


def briefing_cards_dir(output_dir: Path, briefing_id: str) -> Path:
    """Return output/cards/{briefing_id}."""
    safe_id = validate_briefing_id(briefing_id)
    return output_dir / "cards" / safe_id


def validate_briefing_cards(output_dir: Path, briefing_id: str) -> dict[str, Path]:
    """Ensure all three level cards for this briefing exist and are non-empty."""
    cards_dir = briefing_cards_dir(output_dir, briefing_id)

    expected = {
        "elementary": cards_dir / "elementary.png",
        "middle": cards_dir / "middle.png",
        "high": cards_dir / "high.png",
    }

    if not cards_dir.exists():
        raise FileNotFoundError(
            f"브리핑 카드 폴더가 없습니다: {cards_dir}"
        )

    missing = [
        str(path)
        for path in expected.values()
        if not path.exists() or not path.is_file() or path.stat().st_size == 0
    ]

    if missing:
        raise FileNotFoundError(
            "초·중·고 카드 3개가 모두 준비되지 않았습니다.\n"
            + "\n".join(f"- {path}" for path in missing)
        )

    return expected


def pick_newsletter_for_briefing(
    output_dir: Path,
    briefing_id: str,
    level: str,
) -> Path:
    """Pick only the exact level file from this briefing's folder."""
    cards = validate_briefing_cards(output_dir, briefing_id)
    level_key = normalize_header(level)

    if "초등" in level_key or "elementary" in level_key:
        return cards["elementary"]

    if "중학생" in level_key or "중등" in level_key or "middle" in level_key:
        return cards["middle"]

    if "고등" in level_key or "high" in level_key:
        return cards["high"]

    raise ValueError(
        f"알 수 없는 학습 수준입니다: {level!r}. "
        "Google Form의 학습 수준 응답을 확인하세요."
    )


def create_message(
    recipient: str,
    level: str,
    attachment_path: Path,
    sender_name: str,
    site_url: str,
) -> EmailMessage:
    """Create a MIME email with the newsletter attached."""
    today = datetime.now().strftime("%Y.%m.%d")

    msg = EmailMessage()
    msg["To"] = recipient
    msg["Subject"] = f"[MOFA Daily Letter] {today} 외교부 정례브리핑"
    msg["From"] = sender_name

    plain_text = f"""안녕하세요.

MOFA Daily Letter입니다.

신청하신 학습 수준: {level}

오늘의 외교부 정례브리핑을 쉽게 정리한 일간지를 첨부했습니다.
복잡한 외교 이슈를 핵심부터 차근차근 이해해 보세요.

서비스 페이지:
{site_url}

감사합니다.
MOFA Daily Letter
"""
    msg.set_content(plain_text)

    html = f"""
    <html>
      <body>
        <h2>MOFA Daily Letter</h2>
        <p>안녕하세요.</p>
        <p>
          오늘의 <strong>외교부 정례브리핑</strong>을 쉽게 정리한
          일간지를 보내드립니다.
        </p>
        <p><strong>신청하신 학습 수준:</strong> {level}</p>
        <p>첨부된 일간지를 확인해 주세요.</p>
        <p>
          <a href="{site_url}">MOFA Daily Letter 소개 페이지 보기</a>
        </p>
        <hr>
        <p style="font-size: 12px; color: #666;">
          본 메일은 MOFA Daily Letter 구독 신청을 완료한 사용자에게 발송되었습니다.
        </p>
      </body>
    </html>
    """
    msg.add_alternative(html, subtype="html")

    mime_type, _ = mimetypes.guess_type(attachment_path.name)
    if mime_type:
        maintype, subtype = mime_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"

    with attachment_path.open("rb") as f:
        msg.add_attachment(
            f.read(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment_path.name,
        )

    return msg


def send_message(gmail_service: Any, message: EmailMessage) -> str:
    """Send one message with Gmail API and return the Gmail message ID."""
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    result = (
        gmail_service.users()
        .messages()
        .send(
            userId="me",
            body={"raw": encoded},
        )
        .execute()
    )
    return str(result.get("id", ""))


def append_log(
    email: str,
    level: str,
    newsletter: Path,
    status: str,
    detail: str = "",
) -> None:
    """Append a local CSV log row for debugging and backup."""
    new_file = not LOG_FILE.exists()

    with LOG_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(
                ["sent_at", "email", "level", "newsletter", "status", "detail"]
            )

        writer.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                email,
                level,
                newsletter.name,
                status,
                detail,
            ]
        )


def resolve_newsletter_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Google Form 전체 구독자에게 MOFA Daily Letter를 발송합니다."
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="실제로 메일을 발송합니다. 없으면 미리보기만 합니다.",
    )
    parser.add_argument(
        "--briefing-id",
        type=str,
        default=None,
        help="이번에 사용할 정례브리핑의 고유 ID입니다. 미리보기와 실제 발송 모두 필요합니다.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="앞에서부터 N명만 처리합니다. 테스트 발송에 유용합니다.",
    )
    parser.add_argument(
        "--newsletter",
        type=str,
        default=None,
        help="모든 구독자에게 보낼 특정 파일 경로를 지정합니다.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    briefing_id = (args.briefing_id or "").strip()
    if not briefing_id and not args.newsletter:
        raise ValueError(
            "--briefing-id가 필요합니다.\n"
            "예: python send_newsletter.py --briefing-id BRIEF_20260709_368852 --limit 1"
        )

    if briefing_id:
        briefing_id = validate_briefing_id(briefing_id)

    if args.send and args.newsletter:
        raise ValueError(
            "안전을 위해 실제 발송(--send)에서는 --newsletter를 사용할 수 없습니다. "
            "반드시 --briefing-id 폴더의 카드만 발송합니다."
        )

    creds = get_credentials()

    sheets_service = build("sheets", "v4", credentials=creds)
    gmail_service = build("gmail", "v1", credentials=creds)

    subscribers = get_subscribers(
        sheets_service=sheets_service,
        spreadsheet_id=config["spreadsheet_id"],
        sheet_range=config["sheet_range"],
    )

    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit 값은 1 이상이어야 합니다.")
        subscribers = subscribers[: args.limit]

    if not subscribers:
        print("발송 대상 구독자가 없습니다.")
        return

    output_dir = PROJECT_DIR / config["output_dir"]

    fixed_newsletter = (
        resolve_newsletter_path(args.newsletter)
        if args.newsletter
        else None
    )

    if fixed_newsletter and not fixed_newsletter.exists():
        raise FileNotFoundError(f"지정한 파일이 없습니다: {fixed_newsletter}")

    # 자동 발송에서는 루프를 시작하기 전에 해당 브리핑의 카드 3개를 모두 검증합니다.
    if briefing_id and not fixed_newsletter:
        validated_cards = validate_briefing_cards(output_dir, briefing_id)
        print("브리핑 카드 3개 검증 완료")
        print(f"- 초등학생: {validated_cards['elementary']}")
        print(f"- 중학생: {validated_cards['middle']}")
        print(f"- 고등학생: {validated_cards['high']}")

    sent_keys: set[tuple[str, str]] = set()
    if args.send:
        ensure_send_history_sheet(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
        )
        sent_keys = get_sent_keys(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
        )

    print(f"구독자 {len(subscribers)}명 확인")
    print("실제 발송 모드" if args.send else "미리보기 모드(--send 없음)")
    if briefing_id:
        print(f"브리핑 ID: {briefing_id}")

    success_count = 0
    fail_count = 0
    skip_count = 0

    for subscriber in subscribers:
        email = subscriber["email"]
        level = subscriber["level"]

        if args.send and (briefing_id, email) in sent_keys:
            skip_count += 1
            print(f"- {email} | {level} | 이미 발송 완료 | SKIP")
            continue

        newsletter: Path | None = None

        try:
            newsletter = (
                fixed_newsletter
                if fixed_newsletter
                else pick_newsletter_for_briefing(
                    output_dir=output_dir,
                    briefing_id=briefing_id,
                    level=level,
                )
            )

            print(f"- {email} | {level} | 첨부: {newsletter.name}")

            if not args.send:
                continue

            message = create_message(
                recipient=email,
                level=level,
                attachment_path=newsletter,
                sender_name=config["sender_name"],
                site_url=config["site_url"],
            )

            message_id = send_message(gmail_service, message)

            append_log(
                email=email,
                level=level,
                newsletter=newsletter,
                status="SUCCESS",
                detail=message_id,
            )

            append_send_history(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                email=email,
                level=level,
                newsletter=newsletter,
                status="SUCCESS",
                message_id=message_id,
            )

            sent_keys.add((briefing_id, email))
            success_count += 1
            print(f"  발송 성공: {message_id}")

        except Exception as exc:
            fail_count += 1
            newsletter_for_log = newsletter or fixed_newsletter or Path("UNKNOWN")

            append_log(
                email=email,
                level=level,
                newsletter=newsletter_for_log,
                status="FAILED",
                detail=str(exc),
            )

            if args.send:
                try:
                    append_send_history(
                        sheets_service=sheets_service,
                        spreadsheet_id=config["spreadsheet_id"],
                        briefing_id=briefing_id,
                        email=email,
                        level=level,
                        newsletter=newsletter_for_log,
                        status="FAILED",
                        detail=str(exc),
                    )
                except Exception as history_exc:
                    print(f"  send_history 기록 실패: {history_exc}")

            print(f"  발송 실패: {exc}")

    if args.send:
        print(
            "\n완료: "
            f"신규 성공 {success_count}명 / "
            f"이미 발송되어 건너뜀 {skip_count}명 / "
            f"실패 {fail_count}명"
        )
        print(f"로컬 발송 기록: {LOG_FILE}")
        print(f"Google Sheets 발송 기록: {SEND_HISTORY_SHEET}")

        if fail_count > 0:
            raise SystemExit(1)
    else:
        print(
            "\n미리보기 완료. 먼저 1명 실제 테스트를 하려면 아래처럼 실행하세요.\n"
            f"python send_newsletter.py --send --briefing-id {briefing_id} --limit 1"
        )


if __name__ == "__main__":
    main()
