import asyncio
import csv
import json
import os
import re
import traceback
import hmac
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Awaitable

import gspread
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from google.oauth2.service_account import Credentials
from openai import OpenAI
from pydantic import BaseModel
import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
IS_VERCEL = bool(os.getenv("VERCEL"))

# Vercel's deployed code directory is read-only at runtime. Only /tmp is writable.
# Local development keeps the existing folders next to app.py.
RUNTIME_DIR = Path("/tmp/instagram-comment-analyzer") if IS_VERCEL else BASE_DIR
PROFILE_DIR = RUNTIME_DIR / "instagram_browser_profile"
EXPORT_DIR = RUNTIME_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_ID", "1uZj_8iHNIPysfBq_sdmHvcYpxTqvqt0mlRTAXZ6IbsA")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(BASE_DIR / "service_account.json"))
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
INSTAGRAM_WORKER_URL = os.getenv("INSTAGRAM_WORKER_URL", "").rstrip("/")
INSTAGRAM_WORKER_KEY = (os.getenv("WORKER_API_KEY", "") or os.getenv("INSTAGRAM_WORKER_KEY", "")).strip()

app = FastAPI(title="Instagram Comment Analyzer")
app.mount("/api/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# One browser job at a time. This keeps one Instagram profile safe from concurrent writes.
scrape_lock = asyncio.Lock()

# In-memory job progress store. Good for the local/single-process version.
# When deployed to multiple workers, move this to Redis or a database.
jobs: Dict[str, Dict[str, Any]] = {}


def create_job(url: str) -> str:
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "id": job_id,
        "url": url,
        "state": "queued",
        "stage": "대기",
        "message": "작업을 준비하고 있습니다.",
        "progress": 0,
        "current": 0,
        "total": 0,
        "logs": [],
        "error": None,
        "result": None,
    }
    return job_id


def job_update(job_id: str, *, stage: Optional[str] = None, message: Optional[str] = None,
               progress: Optional[int] = None, current: Optional[int] = None,
               total: Optional[int] = None, log: Optional[str] = None,
               state: Optional[str] = None):
    job = jobs.get(job_id)
    if not job:
        return
    if stage is not None: job["stage"] = stage
    if message is not None: job["message"] = message
    if progress is not None: job["progress"] = max(0, min(100, int(progress)))
    if current is not None: job["current"] = current
    if total is not None: job["total"] = total
    if state is not None: job["state"] = state
    if log:
        ts = datetime.now().strftime("%H:%M:%S")
        job["logs"].append(f"[{ts}] {log}")
        job["logs"] = job["logs"][-120:]


class AnalyzeRequest(BaseModel):
    url: str
    client_id: str
    analyze_ai: bool = True
    save_sheet: bool = True
    max_rounds: int = 120


def extract_shortcode(url: str) -> str:
    match = re.search(r"instagram\.com/(?:p|reel)/([^/?#]+)", url)
    if not match:
        raise ValueError("Instagram 게시글 또는 Reel URL이 아닙니다.")
    return match.group(1)


def extract_comment_id(href: str) -> str:
    if not href:
        return ""
    match = re.search(r"/c/(\d+)/", href)
    return match.group(1) if match else ""


async def click_more_comments(page, shortcode: str, max_rounds: int = 120, job_id: Optional[str] = None) -> int:
    """Keep expanding comments until permalink count stops increasing."""
    more_keywords = [
        "댓글 더 보기", "댓글 더 불러오기", "이전 댓글 보기",
        "View more comments", "Load more comments", "View previous comments",
    ]

    selector = f'a[href*="/p/{shortcode}/c/"], a[href*="/reel/{shortcode}/c/"]'
    best_count = await page.locator(selector).count()
    stale_rounds = 0
    if job_id:
        job_update(job_id, stage="댓글 불러오기", message=f"현재 화면에서 댓글 {best_count}개를 확인했습니다.", progress=22, current=best_count, log=f"초기 댓글 permalink {best_count}개 발견")

    for _ in range(max_rounds):
        clicked = False
        candidates = page.locator("button, div[role='button'], span[role='button']")
        count = await candidates.count()

        for i in range(count):
            node = candidates.nth(i)
            try:
                text = (await node.inner_text(timeout=250)).strip()
            except Exception:
                continue
            if text and any(k.lower() in text.lower() for k in more_keywords):
                try:
                    await node.scroll_into_view_if_needed()
                    await node.click(timeout=900)
                    await page.wait_for_timeout(550)
                    clicked = True
                except Exception:
                    pass

        # Scroll to provoke lazy loading even when button text changes.
        try:
            await page.mouse.wheel(0, 1300)
        except Exception:
            pass
        await page.wait_for_timeout(500)

        new_count = await page.locator(selector).count()
        if new_count > best_count:
            best_count = new_count
            stale_rounds = 0
            if job_id:
                pct = min(52, 22 + int(((_ + 1) / max(max_rounds, 1)) * 30))
                job_update(job_id, message=f"댓글을 더 불러오는 중 · {best_count}개 발견", progress=pct, current=best_count, log=f"댓글 수 증가: {best_count}개")
        else:
            stale_rounds += 1

        if not clicked and stale_rounds >= 8:
            break

    if job_id:
        job_update(job_id, message=f"댓글 로딩 완료 · {best_count}개 후보", progress=54, current=best_count, log=f"댓글 로딩 종료: {best_count}개 후보")
    return best_count


async def find_comment_blocks(page, shortcode: str, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Extract comments from permalink anchors.

    Instagram currently renders the comment permalink on the *timestamp* (e.g. "3주").
    So we must not treat the permalink anchor's nearest text block as the comment itself.
    We climb ancestors until we find the smallest block that contains:
      1) a profile/username link, and
      2) at least one non-metadata text line (the actual comment).
    """
    selector = f'a[href*="/p/{shortcode}/c/"], a[href*="/reel/{shortcode}/c/"]'
    links = page.locator(selector)
    count = await links.count()
    if job_id:
        job_update(
            job_id,
            stage="댓글 추출",
            message=f"댓글 후보 {count}개에서 작성자와 실제 댓글 문장을 분리하고 있습니다.",
            progress=56,
            total=count,
            current=0,
            log=f"댓글 permalink {count}개 추출 시작",
        )

    results: List[Dict[str, Any]] = []
    seen_ids = set()
    suspicious = 0

    def is_time_line(line: str) -> bool:
        line = line.strip()
        lower = line.lower()
        return bool(
            re.fullmatch(r"\d+\s*(초|분|시간|일|주|개월|달|년)(\s*전)?", line)
            or re.fullmatch(r"\d+[smhdwy]", lower)
            or re.fullmatch(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일", line)
            or re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", line)
        )

    def is_noise_line(line: str, username: str) -> bool:
        line = line.strip()
        lower = line.lower()
        if not line:
            return True
        if username and line == username:
            return True
        if is_time_line(line):
            return True
        if line in {
            "좋아요", "답글 달기", "번역 보기", "팔로우", "수정됨",
            "Reply", "Like", "See translation", "Follow", "Edited",
        }:
            return True
        if "답글 보기" in line or "답글 숨기기" in line:
            return True
        if "view replies" in lower or "hide replies" in lower:
            return True
        if re.fullmatch(r"좋아요\s*\d+개", line) or re.fullmatch(r"\d+\s*likes?", lower):
            return True
        return False

    async def profile_username(container) -> str:
        # Prefer the username link inside the comment row instead of the first text line.
        anchors = container.locator('a[href]')
        try:
            n = min(await anchors.count(), 30)
        except Exception:
            return ""
        for j in range(n):
            a = anchors.nth(j)
            try:
                href = (await a.get_attribute("href") or "").strip()
                txt = (await a.inner_text(timeout=250)).strip()
            except Exception:
                continue
            # Profile URLs look like /username/; exclude all known route families.
            if not re.fullmatch(r"/[A-Za-z0-9._]+/", href):
                continue
            first = href.strip("/")
            if first in {
                "p", "reel", "reels", "explore", "direct", "accounts", "stories",
                "legal", "popular", "web", "about", "developer",
            }:
                continue
            if txt and len(txt) <= 80:
                return txt
            return first
        return ""

    for i in range(count):
        if job_id and (i % 10 == 0 or i == count - 1):
            pct = 56 + int(((i + 1) / max(count, 1)) * 9)
            job_update(
                job_id,
                progress=pct,
                current=i + 1,
                total=count,
                message=f"댓글 텍스트 분리 중 · {i + 1}/{count}",
            )

        link = links.nth(i)
        try:
            href = await link.get_attribute("href")
        except Exception:
            continue

        comment_id = extract_comment_id(href or "")
        if not comment_id or comment_id in seen_ids:
            continue
        seen_ids.add(comment_id)

        block = link
        chosen = None

        # The nearest parent usually only has username + timestamp. Keep climbing until
        # a genuine non-metadata comment line appears, but stop before the whole post.
        for level in range(1, 13):
            try:
                block = block.locator("..")
                text = (await block.inner_text(timeout=700)).strip()
            except Exception:
                break
            if not text or len(text) > 2400:
                continue

            lines = [x.strip() for x in text.split("\n") if x.strip()]
            if len(lines) < 2:
                continue

            username = await profile_username(block)
            if not username:
                continue

            created_at = next((x for x in lines if is_time_line(x)), "")
            likes = next(
                (
                    x
                    for x in lines
                    if re.fullmatch(r"좋아요\s*\d+개", x)
                    or re.fullmatch(r"\d+\s*likes?", x.lower())
                ),
                "",
            )
            comment_lines = [x for x in lines if not is_noise_line(x, username)]

            # Some ancestors contain accessibility duplicates; keep order but de-dupe.
            de_duped = []
            for x in comment_lines:
                if x not in de_duped:
                    de_duped.append(x)
            comment_lines = de_duped

            if comment_lines:
                comment = " ".join(comment_lines).strip()
                # A comment must not itself be only a relative date such as "3주".
                if comment and not is_time_line(comment):
                    chosen = (username, comment, created_at, likes, level, lines)
                    break

        if not chosen:
            suspicious += 1
            if job_id and suspicious <= 5:
                job_update(job_id, log=f"댓글 {comment_id}: 텍스트 분리에 실패하여 건너뜀")
            continue

        username, comment, created_at, likes, level, raw_lines = chosen
        results.append({
            "comment_id": comment_id,
            "username": username,
            "comment": comment,
            "created_at": created_at,
            "likes": likes,
            "permalink": ("https://www.instagram.com" + href) if href and href.startswith("/") else (href or ""),
        })

        if job_id and len(results) <= 3:
            job_update(
                job_id,
                log=f"댓글 샘플 {len(results)}: @{username} → {comment[:60]}",
            )

    if job_id:
        job_update(
            job_id,
            message=f"실제 댓글 {len(results)}개 추출 완료" + (f" · 분리 실패 {suspicious}개" if suspicious else ""),
            progress=65,
            current=len(results),
            total=count,
            log=f"댓글 추출 완료: 성공 {len(results)}개 / 실패 {suspicious}개",
        )
    return results


async def scrape_comments(post_url: str, max_rounds: int, client_id: str, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    extract_shortcode(post_url)
    if not INSTAGRAM_WORKER_URL:
        raise HTTPException(status_code=503, detail="INSTAGRAM_WORKER_URL이 설정되지 않았습니다.")
    if not client_id or len(client_id) < 8:
        raise HTTPException(status_code=400, detail="Instagram 사용자 세션 ID가 없습니다. 먼저 Instagram 로그인을 진행해 주세요.")
    if job_id:
        job_update(job_id, state="running", stage="Instagram 연결", message="원격 Instagram 세션을 확인하고 있습니다.", progress=8, log="Instagram Worker 연결 시작")
    headers = {}
    if INSTAGRAM_WORKER_KEY:
        headers["X-Worker-Key"] = INSTAGRAM_WORKER_KEY
    payload = {"client_id": client_id, "url": post_url, "max_rounds": max_rounds}
    timeout = httpx.Timeout(240.0, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if job_id:
                job_update(job_id, stage="댓글 불러오기", message="로그인된 원격 브라우저에서 댓글을 수집하고 있습니다.", progress=20, log="Worker /scrape 요청")
            r = await client.post(f"{INSTAGRAM_WORKER_URL}/scrape", json=payload, headers=headers)
            data = r.json() if "application/json" in r.headers.get("content-type", "") else {"detail": r.text[:500]}
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail=data.get("detail", "Instagram 로그인이 필요합니다."))
            if r.status_code >= 400:
                raise HTTPException(status_code=503, detail=data.get("detail", f"Instagram Worker 오류 {r.status_code}"))
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Instagram Worker 연결 실패: {e}")
    comments = data.get("comments", [])
    if job_id:
        job_update(job_id, stage="댓글 추출", message=f"실제 댓글 {len(comments)}개 수집 완료", progress=65, current=len(comments), total=len(comments), log=f"Worker 댓글 수집 완료: {len(comments)}개")
    return comments


def ai_client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    return OpenAI()


def _empty_ai_result(message: str = "") -> Dict[str, str]:
    return {
        "sentiment": "",
        "comment_type": "",
        "keyword": "",
        "purchase_intent": "",
        "ai_summary": message,
    }


def analyze_batch(batch: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Analyze many comments in a single OpenAI request.

    We send a local numeric index instead of usernames/IDs so the model only needs
    the text required for classification. The returned array is mapped back by index.
    """
    client = ai_client()
    payload = [
        {"index": i, "comment": str(item.get("comment", ""))}
        for i, item in enumerate(batch)
    ]
    prompt = f"""
너는 인스타그램 광고 댓글 분석기다.
아래 댓글 배열을 각각 독립적으로 분석해라.

반드시 JSON 객체 하나만 반환하고, 최상위 키는 "results"로 해라.
results의 각 항목은 입력의 index를 그대로 포함해야 한다.

분류 기준:
- sentiment: 긍정 | 중립 | 부정
- comment_type: 구매문의 | 제품문의 | 사용후기 | 가격불만 | 효과의심 | 광고비판 | 배송문의 | 재구매 | 자극·부작용 언급 | 기타
- purchase_intent: 높음 | 중간 | 낮음
- keyword: 핵심 키워드 1~3개를 짧은 문자열로
- ai_summary: 댓글 의미를 아주 짧은 한 문장으로

출력 예시:
{{
  "results": [
    {{
      "index": 0,
      "sentiment": "중립",
      "comment_type": "구매문의",
      "keyword": "가격, 구매",
      "purchase_intent": "높음",
      "ai_summary": "가격을 문의하며 구매 관심을 보임"
    }}
  ]
}}

입력 댓글:
{json.dumps(payload, ensure_ascii=False)}
"""
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        text = response.output_text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        raw_results = data.get("results", []) if isinstance(data, dict) else []
        mapped: Dict[int, Dict[str, str]] = {}
        for row in raw_results:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("index"))
            except Exception:
                continue
            mapped[idx] = {
                "sentiment": str(row.get("sentiment", "")),
                "comment_type": str(row.get("comment_type", "")),
                "keyword": str(row.get("keyword", "")),
                "purchase_intent": str(row.get("purchase_intent", "")),
                "ai_summary": str(row.get("ai_summary", "")),
            }
        return [mapped.get(i, _empty_ai_result("AI 분석 결과 누락")) for i in range(len(batch))]
    except Exception as e:
        return [_empty_ai_result(f"AI 분석 실패: {e}") for _ in batch]


async def analyze_comments(comments: List[Dict[str, Any]], job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    # Batch comments to reduce API calls, latency, and cost. Override with env if needed.
    total = len(comments)
    batch_size = max(1, min(50, int(os.getenv("OPENAI_BATCH_SIZE", "20"))))
    total_batches = (total + batch_size - 1) // batch_size if total else 0
    if job_id:
        job_update(
            job_id,
            stage="AI 분석",
            message=f"댓글 {total}개를 {batch_size}개씩 묶어 분석합니다 · 총 {total_batches}배치",
            progress=67,
            current=0,
            total=total,
            log=f"OpenAI 배치 분석 시작 · 모델 {OPENAI_MODEL} · 배치 크기 {batch_size}",
        )

    failures = 0
    processed = 0
    for batch_no, start_idx in enumerate(range(0, total, batch_size), start=1):
        batch = comments[start_idx:start_idx + batch_size]
        if job_id:
            job_update(
                job_id,
                message=f"AI 분석 중 · 배치 {batch_no}/{total_batches} · 댓글 {processed}/{total}",
                log=f"AI 배치 {batch_no}/{total_batches} 요청 · {len(batch)}개 댓글",
            )
        results = await asyncio.to_thread(analyze_batch, batch)
        for item, result in zip(batch, results):
            item.update(result)
            if str(result.get("ai_summary", "")).startswith("AI 분석 실패") or str(result.get("ai_summary", "")) == "AI 분석 결과 누락":
                failures += 1
        processed += len(batch)
        if job_id:
            pct = 67 + int((processed / max(total, 1)) * 23)
            job_update(
                job_id,
                progress=pct,
                current=processed,
                total=total,
                message=f"AI 분석 중 · {processed}/{total} · 배치 {batch_no}/{total_batches}",
                log=f"AI 배치 {batch_no}/{total_batches} 완료 · 누적 실패 {failures}",
            )

    if job_id:
        job_update(
            job_id,
            message=f"AI 분석 완료 · {total - failures}/{total} 성공 · API 호출 {total_batches}회",
            progress=90,
            log=f"OpenAI 배치 분석 종료 · API 호출 {total_batches}회 · 실패 {failures}개",
        )
    return comments

def connect_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Production/Vercel: keep the service-account secret in an environment variable.
    # Local: service_account.json is still supported for convenience.
    if SERVICE_ACCOUNT_JSON:
        try:
            info = json.loads(SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_JSON이 올바른 JSON이 아닙니다: {e}")
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
    elif Path(SERVICE_ACCOUNT_FILE).exists():
        credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    else:
        raise RuntimeError(
            "Google 서비스 계정이 설정되지 않았습니다. "
            "Vercel에서는 GOOGLE_SERVICE_ACCOUNT_JSON 환경변수를 추가해 주세요."
        )

    gc = gspread.authorize(credentials)
    return gc.open_by_key(SPREADSHEET_ID).sheet1


# Google Sheet uses these display headers. Values are mapped by header NAME, not by
# a hard-coded column position. This prevents columns from shifting when the app's
# internal data structure changes.
SHEET_HEADER_TO_FIELD = {
    "게시글 URL": "post_url",
    "작성자": "username",
    "댓글": "comment",
    "작성일": "created_at",
    "좋아요": "likes",
    "긍부정": "sentiment",
    "댓글유형": "comment_type",
    "핵심키워드": "keyword",
    "구매의향": "purchase_intent",
    "AI 요약": "ai_summary",
    "comment_id": "comment_id",
}

DEFAULT_SHEET_HEADERS = list(SHEET_HEADER_TO_FIELD.keys())


def save_to_sheet(post_url: str, comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    ws = connect_sheet()

    # 1) Read the ACTUAL first row from Google Sheets.
    sheet_headers = [str(x).strip() for x in ws.row_values(1)]
    if not any(sheet_headers):
        sheet_headers = DEFAULT_SHEET_HEADERS.copy()
        ws.update("A1", [sheet_headers], value_input_option="USER_ENTERED")

    # Trim only empty columns at the right edge. Empty columns in the middle are kept
    # because they are part of the user's sheet layout.
    while sheet_headers and not sheet_headers[-1]:
        sheet_headers.pop()

    if not sheet_headers:
        raise RuntimeError("Google Sheets의 1행 헤더를 읽지 못했습니다.")

    unknown_headers = [h for h in sheet_headers if h and h not in SHEET_HEADER_TO_FIELD]
    missing_required = [h for h in DEFAULT_SHEET_HEADERS if h not in sheet_headers]

    # Unknown columns are intentionally preserved as blanks. Missing known columns are
    # reported so the UI can show exactly what the sheet looks like. We do NOT silently
    # reorder or overwrite the user's header row.

    # 2) Deduplicate using comment_id regardless of which column letter it occupies.
    existing = set()
    try:
        records = ws.get_all_records(expected_headers=sheet_headers)
        for record in records:
            cid = str(record.get("comment_id", "")).strip()
            if cid:
                existing.add(cid)
    except Exception:
        # Fallback for older gspread versions.
        values = ws.get_all_values()
        try:
            cid_index = sheet_headers.index("comment_id")
        except ValueError:
            cid_index = -1
        if cid_index >= 0:
            for row in values[1:]:
                if cid_index < len(row):
                    cid = str(row[cid_index]).strip()
                    if cid:
                        existing.add(cid)

    # 3) Build each row by looking up the field for EACH sheet header.
    rows = []
    for item in comments:
        comment_id = str(item.get("comment_id", "")).strip()
        if comment_id and comment_id in existing:
            continue

        data = {
            "post_url": post_url,
            "username": item.get("username", ""),
            "comment": item.get("comment", ""),
            "created_at": item.get("created_at", ""),
            "likes": item.get("likes", ""),
            "sentiment": item.get("sentiment", ""),
            "comment_type": item.get("comment_type", ""),
            "keyword": item.get("keyword", ""),
            "purchase_intent": item.get("purchase_intent", ""),
            "ai_summary": item.get("ai_summary", ""),
            "comment_id": comment_id,
        }

        row = []
        for header in sheet_headers:
            field = SHEET_HEADER_TO_FIELD.get(header)
            row.append(data.get(field, "") if field else "")
        rows.append(row)

        if comment_id:
            existing.add(comment_id)

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")

    return {
        "saved": len(rows),
        "columns": len(sheet_headers),
        "headers": sheet_headers,
        "unknown_headers": unknown_headers,
        "missing_headers": missing_required,
    }


def export_csv(post_url: str, comments: List[Dict[str, Any]]) -> Path:
    filename = f"comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    path = EXPORT_DIR / filename
    fields = [
        "post_url", "comment_id", "username", "comment", "created_at", "likes",
        "sentiment", "comment_type", "keyword", "purchase_intent", "ai_summary", "permalink",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in comments:
            writer.writerow({"post_url": post_url, **{k: item.get(k, "") for k in fields if k != "post_url"}})
    return path


def summarize(comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    sentiments = {"긍정": 0, "중립": 0, "부정": 0}
    types: Dict[str, int] = {}
    intents = {"높음": 0, "중간": 0, "낮음": 0}

    for c in comments:
        s = c.get("sentiment", "")
        if s in sentiments:
            sentiments[s] += 1
        t = c.get("comment_type", "")
        if t:
            types[t] = types.get(t, 0) + 1
        pi = c.get("purchase_intent", "")
        if pi in intents:
            intents[pi] += 1

    return {
        "total": len(comments),
        "sentiments": sentiments,
        "types": dict(sorted(types.items(), key=lambda kv: kv[1], reverse=True)),
        "purchase_intent": intents,
    }


def get_system_status() -> Dict[str, Any]:
    openai_key = bool(os.getenv("OPENAI_API_KEY", "").strip())

    sheet = {
        "ok": False,
        "label": "미연결",
        "detail": "service_account.json을 확인하지 못했습니다.",
    }
    try:
        ws = connect_sheet()
        headers = [str(x).strip() for x in ws.row_values(1)]
        sheet = {
            "ok": True,
            "label": "연결됨",
            "detail": f"Google Sheets 연결 확인 · {len(headers)}개 헤더",
        }
    except Exception as e:
        sheet = {"ok": False, "label": "오류", "detail": str(e)}

    worker_configured = bool(INSTAGRAM_WORKER_URL)

    return {
        "openai": {
            "ok": openai_key,
            "label": "설정됨" if openai_key else "API 키 없음",
            "detail": f"모델: {OPENAI_MODEL}" if openai_key else "OPENAI_API_KEY 환경변수를 설정해 주세요.",
        },
        "sheets": sheet,
        "instagram": {
            "ok": worker_configured,
            "label": "Worker 설정됨" if worker_configured else "Worker 미설정",
            "detail": "사용자별 로그인 상태는 브라우저에서 별도로 확인합니다." if worker_configured else "INSTAGRAM_WORKER_URL 환경변수를 설정해 주세요.",
        },
    }


@app.get("/api/health")
async def health():
    return {"ok": True, "vercel": IS_VERCEL, "runtime_dir": str(RUNTIME_DIR)}


@app.get("/api/system-status")
async def system_status():
    return get_system_status()


@app.get("/", response_class=HTMLResponse)
@app.get("/api", response_class=HTMLResponse)
async def home():
    return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/instagram-login-url")
async def instagram_login_url(client_id: str, return_to: str = ""):
    if not INSTAGRAM_WORKER_URL:
        raise HTTPException(status_code=503, detail="INSTAGRAM_WORKER_URL이 설정되지 않았습니다.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_id or ""):
        raise HTTPException(status_code=400, detail="잘못된 client_id입니다.")
    from urllib.parse import urlencode
    sig = hmac.new(INSTAGRAM_WORKER_KEY.encode(), client_id.encode(), hashlib.sha256).hexdigest() if INSTAGRAM_WORKER_KEY else ""
    q = urlencode({"client_id": client_id, "return_to": return_to, "sig": sig})
    return {"url": f"{INSTAGRAM_WORKER_URL}/login?{q}"}


@app.get("/api/instagram-status")
async def instagram_status(client_id: str):
    if not INSTAGRAM_WORKER_URL:
        return {"ok": False, "logged_in": False, "label": "Worker 미설정", "detail": "INSTAGRAM_WORKER_URL을 설정해 주세요."}
    headers = {"X-Worker-Key": INSTAGRAM_WORKER_KEY} if INSTAGRAM_WORKER_KEY else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{INSTAGRAM_WORKER_URL}/profile/status", params={"client_id": client_id}, headers=headers)
            data = r.json()
            return {"ok": r.is_success, "logged_in": bool(data.get("logged_in")), "label": "로그인됨" if data.get("logged_in") else "로그인 필요", "detail": data.get("detail", "")}
    except Exception as e:
        return {"ok": False, "logged_in": False, "label": "Worker 연결 실패", "detail": str(e)}


@app.post("/api/jobs")
async def create_analysis_job(req: AnalyzeRequest):
    try:
        extract_shortcode(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_id = create_job(req.url)
    asyncio.create_task(run_analysis_job(job_id, req))
    return {"job_id": job_id}


async def run_analysis_job(job_id: str, req: AnalyzeRequest):
    try:
        job_update(job_id, state="running", stage="준비", message="분석 작업을 시작합니다.", progress=2, log="작업 시작")
        comments = await scrape_comments(req.url, req.max_rounds, req.client_id, job_id=job_id)
        if not comments:
            job_update(job_id, log="수집된 댓글이 0개입니다.")

        if req.analyze_ai and comments:
            comments = await analyze_comments(comments, job_id=job_id)
        elif comments:
            job_update(job_id, stage="AI 분석", message="AI 분석 옵션이 꺼져 있어 건너뜁니다.", progress=90, log="AI 분석 건너뜀")

        saved = 0
        sheet_error = None
        sheet_info = None
        if req.save_sheet and comments:
            try:
                job_update(job_id, stage="Google Sheets 저장", message="시트 1행 헤더를 읽고 컬럼 이름에 맞춰 저장하고 있습니다.", progress=92, log="Google Sheets 연결 시작 · 1행 헤더 확인")
                sheet_info = await asyncio.to_thread(save_to_sheet, req.url, comments)
                saved = int(sheet_info.get("saved", 0))
                columns = int(sheet_info.get("columns", 0))
                headers = sheet_info.get("headers", [])
                job_update(job_id, log="시트 헤더: " + " | ".join(headers))
                if sheet_info.get("unknown_headers"):
                    job_update(job_id, log="알 수 없는 시트 컬럼(빈 값으로 유지): " + ", ".join(sheet_info["unknown_headers"]))
                if sheet_info.get("missing_headers"):
                    job_update(job_id, log="시트에 없는 표준 컬럼: " + ", ".join(sheet_info["missing_headers"]))
                job_update(job_id, message=f"Google Sheets 저장 완료 · {saved}개 행 × {columns}개 컬럼", progress=96, log=f"Google Sheets 저장 완료: {saved}개 행 / {columns}개 컬럼")
            except Exception as e:
                sheet_error = str(e)
                job_update(job_id, message="Google Sheets 저장에는 실패했지만 결과 생성은 계속합니다.", progress=96, log=f"Google Sheets 저장 실패: {sheet_error}")
        else:
            job_update(job_id, stage="결과 생성", message="결과 파일을 만드는 중입니다.", progress=94, log="Google Sheets 저장 건너뜀")

        job_update(job_id, stage="결과 생성", message="CSV와 요약 결과를 생성하고 있습니다.", progress=97, log="CSV 생성 시작")
        export_path = export_csv(req.url, comments)
        summary = summarize(comments)

        result = {
            "ok": True,
            "summary": summary,
            "comments": comments,
            "sheet_saved": saved,
            "sheet_info": sheet_info,
            "sheet_error": sheet_error,
            "download_url": f"/api/download/{export_path.name}",
        }
        jobs[job_id]["result"] = result
        job_update(job_id, state="done", stage="완료", message=f"완료 · 댓글 {len(comments)}개 처리", progress=100, current=len(comments), total=len(comments), log="전체 작업 완료")
    except HTTPException as e:
        jobs[job_id]["error"] = e.detail
        job_update(job_id, state="error", stage=jobs[job_id].get("stage") or "오류", message=str(e.detail), log=f"오류: {e.detail}")
    except Exception as e:
        detail = str(e) or e.__class__.__name__
        jobs[job_id]["error"] = detail
        job_update(job_id, state="error", message=detail, log=f"예외: {detail}")
        print(traceback.format_exc())


@app.get("/api/jobs/{job_id}")
async def get_analysis_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return job


@app.get("/api/download/{filename}")
async def download(filename: str):
    path = EXPORT_DIR / filename
    if not path.exists() or path.parent != EXPORT_DIR:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="text/csv", filename=filename)
