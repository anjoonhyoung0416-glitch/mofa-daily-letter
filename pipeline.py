from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
import sys
from typing import Any

from googleapiclient.discovery import build

from run_mvp import collect_items_from_mofa, generate_latest_briefing
from send_newsletter import get_credentials, load_config


BRIEFING_STATE_SHEET = "briefing_state"
BRIEFING_STATE_RANGE = f"{BRIEFING_STATE_SHEET}!A:H"

BRIEFING_STATE_HEADERS = [
    "briefing_id",
    "title",
    "briefing_date",
    "source_url",
    "status",
    "detected_at",
    "updated_at",
    "detail",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_briefing_state_sheet(
    sheets_service: Any,
    spreadsheet_id: str,
) -> None:
    """
    briefing_state 시트가 없으면 자동 생성하고,
    비어 있으면 첫 행에 헤더를 작성합니다.
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

    if BRIEFING_STATE_SHEET not in titles:
        print(f"'{BRIEFING_STATE_SHEET}' 시트가 없어 자동 생성합니다.")

        (
            sheets_service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": BRIEFING_STATE_SHEET,
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
            range=BRIEFING_STATE_RANGE,
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
            range=f"{BRIEFING_STATE_SHEET}!A1:H1",
            valueInputOption="RAW",
            body={"values": [BRIEFING_STATE_HEADERS]},
        )
        .execute()
    )


def get_briefing_state(
    sheets_service: Any,
    spreadsheet_id: str,
    briefing_id: str,
) -> dict[str, str] | None:
    """
    briefing_state에서 해당 briefing_id의 가장 최근 상태를 찾습니다.
    """
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=BRIEFING_STATE_RANGE,
        )
        .execute()
    )

    rows = result.get("values", [])

    if len(rows) < 2:
        return None

    for row in reversed(rows[1:]):
        row_briefing_id = row[0].strip() if len(row) > 0 else ""

        if row_briefing_id != briefing_id:
            continue

        return {
            "briefing_id": row[0].strip() if len(row) > 0 else "",
            "title": row[1].strip() if len(row) > 1 else "",
            "briefing_date": row[2].strip() if len(row) > 2 else "",
            "source_url": row[3].strip() if len(row) > 3 else "",
            "status": row[4].strip().upper() if len(row) > 4 else "",
            "detected_at": row[5].strip() if len(row) > 5 else "",
            "updated_at": row[6].strip() if len(row) > 6 else "",
            "detail": row[7].strip() if len(row) > 7 else "",
        }

    return None


def append_briefing_state(
    sheets_service: Any,
    spreadsheet_id: str,
    *,
    briefing_id: str,
    title: str,
    briefing_date: str,
    source_url: str,
    status: str,
    detected_at: str | None = None,
    detail: str = "",
) -> None:
    """
    briefing_state에 상태 변경 기록 1행을 추가합니다.
    detected_at은 최초 감지 시각을 유지합니다.
    """
    timestamp = now_iso()
    first_detected_at = detected_at or timestamp

    values = [[
        briefing_id,
        title,
        briefing_date,
        source_url,
        status,
        first_detected_at,
        timestamp,
        detail,
    ]]

    (
        sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=BRIEFING_STATE_RANGE,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


def verify_generation_result(
    result: dict,
    expected_briefing_id: str,
) -> None:
    """
    run_mvp.py 결과가 현재 처리 중인 브리핑과 일치하고,
    초·중·고 카드 파일이 실제 존재하는지 최종 확인합니다.
    """
    actual_briefing_id = str(result.get("briefing_id", "")).strip()

    if actual_briefing_id != expected_briefing_id:
        raise RuntimeError(
            "브리핑 ID 불일치: "
            f"감지={expected_briefing_id}, 생성={actual_briefing_id}"
        )

    files = result.get("files", {})

    required_levels = ["초등학생", "중학생", "고등학생"]

    for level in required_levels:
        file_value = files.get(level)

        if not file_value:
            raise FileNotFoundError(
                f"{level} 카드 파일 경로가 생성 결과에 없습니다."
            )

        path = Path(file_value)

        if not path.exists():
            raise FileNotFoundError(
                f"{level} 카드 파일이 실제로 존재하지 않습니다: {path}"
            )

        if path.stat().st_size == 0:
            raise ValueError(
                f"{level} 카드 파일 크기가 0입니다: {path}"
            )


def run_newsletter_full_send(briefing_id: str) -> None:
    """
    send_newsletter.py를 실제 전체 발송 모드로 실행합니다.

    send_newsletter.py 내부의 send_history 중복 방지 로직에 따라
    이미 SUCCESS인 (briefing_id, email) 조합은 SKIP되고,
    실패했던 대상자만 다시 시도됩니다.
    """
    command = [
        sys.executable,
        "send_newsletter.py",
        "--send",
        "--briefing-id",
        briefing_id,
    ]

    print("\n=== newsletter 전체 발송 시작 ===")
    print("send_history의 SUCCESS 대상자는 자동으로 SKIP됩니다.")

    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parent,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"send_newsletter.py 전체 발송 실패 "
            f"(종료 코드 {result.returncode})"
        )

    print("=== newsletter 전체 발송 성공 ===")

def main() -> None:
    print("=== MOFA Daily Letter Pipeline 전체 자동화 실행 ===")
    print("새 브리핑 감지 → 카드 생성 → 전체 발송 → 상태 완료를 관리합니다.\n")

    config = load_config()
    creds = get_credentials()

    sheets_service = build(
        "sheets",
        "v4",
        credentials=creds,
    )

    ensure_briefing_state_sheet(
        sheets_service=sheets_service,
        spreadsheet_id=config["spreadsheet_id"],
    )

    print("외교부 최신 정례브리핑 확인 중...")
    item = collect_items_from_mofa(limit=1)[0]

    briefing_id = item["briefing_id"]

    print("\n최신 브리핑 확인 완료")
    print("브리핑 ID:", briefing_id)
    print("제목:", item["title"])
    print("작성일:", item["date"])

    current_state = get_briefing_state(
        sheets_service=sheets_service,
        spreadsheet_id=config["spreadsheet_id"],
        briefing_id=briefing_id,
    )

    if current_state is None:
        append_briefing_state(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
            briefing_id=briefing_id,
            title=item["title"],
            briefing_date=item["date"],
            source_url=item["link"],
            status="DETECTED",
            detail="최신 정례브리핑 자동 감지",
        )

        current_state = get_briefing_state(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
            briefing_id=briefing_id,
        )

        if current_state is None:
            raise RuntimeError("DETECTED 상태 기록 후 상태를 다시 읽지 못했습니다.")

        print("\n새 브리핑 감지")
        print("상태: DETECTED")
    else:
        print("\n기존 브리핑 상태 확인")
        print("현재 상태:", current_state["status"])

    status = current_state["status"]
    detected_at = current_state["detected_at"]

    if status == "COMPLETED":
        print("\n이미 COMPLETED된 브리핑입니다.")
        print("Gemini 생성과 이메일 발송을 모두 건너뜁니다.")
        return

    if status in {"READY", "SENDING", "RETRY_SEND"}:
        print(f"\n현재 상태: {status}")
        print("카드는 이미 준비되어 있으므로 Gemini를 다시 호출하지 않습니다.")

        if status == "READY":
            append_briefing_state(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                title=item["title"],
                briefing_date=item["date"],
                source_url=item["link"],
                status="SENDING",
                detected_at=detected_at,
                detail="전체 구독자 발송 시작",
            )
            print("상태 변경: SENDING")
        else:
            print("이전 발송 작업을 이어서 재시도합니다.")

        try:
            run_newsletter_full_send(briefing_id=briefing_id)

            append_briefing_state(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                title=item["title"],
                briefing_date=item["date"],
                source_url=item["link"],
                status="COMPLETED",
                detected_at=detected_at,
                detail="전체 구독자 발송 완료",
            )

            print("\n=== 전체 파이프라인 완료 ===")
            print("브리핑 ID:", briefing_id)
            print("상태: COMPLETED")
            return

        except Exception as exc:
            append_briefing_state(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                title=item["title"],
                briefing_date=item["date"],
                source_url=item["link"],
                status="RETRY_SEND",
                detected_at=detected_at,
                detail=str(exc),
            )

            print("\n전체 발송 단계 실패")
            print("상태: RETRY_SEND")
            print("오류:", exc)
            print("다음 실행에서는 SUCCESS 대상자는 SKIP하고 실패 대상자만 재시도합니다.")
            raise SystemExit(1)

    if status not in {
        "DETECTED",
        "GENERATING",
        "RETRY_GENERATION",
        "FAILED",
    }:
        raise RuntimeError(
            f"처리 방법이 정의되지 않은 상태입니다: {status}"
        )

    append_briefing_state(
        sheets_service=sheets_service,
        spreadsheet_id=config["spreadsheet_id"],
        briefing_id=briefing_id,
        title=item["title"],
        briefing_date=item["date"],
        source_url=item["link"],
        status="GENERATING",
        detected_at=detected_at,
        detail="초·중·고 카드뉴스 생성 시작",
    )

    print("\n상태 변경: GENERATING")
    print("카드뉴스 생성 또는 기존 완성본 검증을 시작합니다.")

    try:
        result = generate_latest_briefing()

        verify_generation_result(
            result=result,
            expected_briefing_id=briefing_id,
        )

        reused = bool(result.get("reused", False))
        detail = (
            "기존 완성 카드 3개 검증 및 재사용"
            if reused
            else "초·중·고 카드 3개 생성 및 검증 완료"
        )

        append_briefing_state(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
            briefing_id=briefing_id,
            title=item["title"],
            briefing_date=item["date"],
            source_url=item["link"],
            status="READY",
            detected_at=detected_at,
            detail=detail,
        )

        print("\n=== 카드 생성 단계 성공 ===")
        print("브리핑 ID:", briefing_id)
        print("상태: READY")
        print("카드 폴더:", result["cards_dir"])
        print("처리 방식:", "기존 완성본 재사용" if reused else "새로 생성")

        append_briefing_state(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
            briefing_id=briefing_id,
            title=item["title"],
            briefing_date=item["date"],
            source_url=item["link"],
            status="SENDING",
            detected_at=detected_at,
            detail="전체 구독자 발송 시작",
        )

        print("\n상태 변경: SENDING")

        try:
            run_newsletter_full_send(briefing_id=briefing_id)

            append_briefing_state(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                title=item["title"],
                briefing_date=item["date"],
                source_url=item["link"],
                status="COMPLETED",
                detected_at=detected_at,
                detail="전체 구독자 발송 완료",
            )

            print("\n=== 전체 파이프라인 완료 ===")
            print("브리핑 ID:", briefing_id)
            print("상태: COMPLETED")

        except Exception as exc:
            append_briefing_state(
                sheets_service=sheets_service,
                spreadsheet_id=config["spreadsheet_id"],
                briefing_id=briefing_id,
                title=item["title"],
                briefing_date=item["date"],
                source_url=item["link"],
                status="RETRY_SEND",
                detected_at=detected_at,
                detail=str(exc),
            )

            print("\n전체 발송 단계 실패")
            print("상태: RETRY_SEND")
            print("오류:", exc)
            print("다음 실행에서는 SUCCESS 대상자는 SKIP하고 실패 대상자만 재시도합니다.")
            raise SystemExit(1)

    except Exception as exc:
        append_briefing_state(
            sheets_service=sheets_service,
            spreadsheet_id=config["spreadsheet_id"],
            briefing_id=briefing_id,
            title=item["title"],
            briefing_date=item["date"],
            source_url=item["link"],
            status="RETRY_GENERATION",
            detected_at=detected_at,
            detail=str(exc),
        )

        print("\n카드 생성 단계 실패")
        print("상태: RETRY_GENERATION")
        print("오류:", exc)
        print("이메일은 발송하지 않았습니다.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
