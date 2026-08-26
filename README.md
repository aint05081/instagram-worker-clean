# Instagram Comment Analyzer v6 — Vercel routing fix

Vercel용 FastAPI 엔트리포인트를 `api/index.py`로 이동한 버전입니다.

## Vercel Environment Variables

- `OPENAI_API_KEY`
- `OPENAI_MODEL` (optional, default: `gpt-5-mini`)
- `GOOGLE_SHEETS_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON` — service_account.json 전체 JSON 문자열

`GOOGLE_SERVICE_ACCOUNT_FILE`은 Vercel에서는 사용하지 않아도 됩니다.

## 확인 주소

- `/` — 사이트 화면 (Vercel rewrite → `/api`)
- `/api/health` — 런타임 상태
- `/api/system-status` — OpenAI / Google Sheets / Instagram 상태

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export OPENAI_API_KEY="..."
uvicorn api.index:app --reload --host 127.0.0.1 --port 8000
```

로컬에서는 `http://127.0.0.1:8000/api`를 열 수 있습니다.

## 주의

Vercel은 지속적인 Playwright 로그인 프로필 보관에 적합하지 않습니다. 이 버전은 사이트/OpenAI/Google Sheets가 정상 기동하도록 Vercel 구조를 맞춘 버전이며, Instagram 로그인 세션 기반 수집은 외부 지속형 수집 서버 연결이 필요합니다.
