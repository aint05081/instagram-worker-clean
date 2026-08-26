# Instagram Comment Analyzer v10 — Deployed Site Login

구성:

- `vercel/`: 사용자 화면 + OpenAI 분석 + Google Sheets 저장
- `worker/`: 사용자별 Instagram 원격 브라우저 로그인 + 댓글 수집

사용자는 로컬 앱/확장프로그램을 설치하지 않습니다.
배포된 사이트에서 **Instagram 로그인** 버튼을 누르면 Worker의 원격 브라우저 화면으로 이동해 직접 로그인합니다.

## 1. Worker 배포

`worker/`는 Docker로 배포합니다. Railway / Render / VPS 등 Docker + persistent disk가 가능한 환경을 사용하세요.

필수 환경변수:

- `WORKER_API_KEY`: 길고 랜덤한 비밀 문자열
- `PROFILE_ROOT=/data/profiles`

중요: `/data`에 persistent volume/disk를 연결해야 Instagram 로그인 세션이 재배포/재시작 후에도 유지됩니다.

배포 후 확인:

`https://WORKER주소/health`

응답 예: `{"ok":true,...}`

## 2. Vercel 환경변수

기존 값:

- `OPENAI_API_KEY`
- `OPENAI_MODEL=gpt-5-mini`
- `GOOGLE_SHEETS_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

추가 값:

- `INSTAGRAM_WORKER_URL=https://WORKER주소`
- `INSTAGRAM_WORKER_KEY=Worker의 WORKER_API_KEY와 정확히 같은 값`

환경변수 저장 후 Redeploy 하세요.

## 3. 사용

1. Vercel 사이트 접속
2. `Instagram 로그인` 클릭
3. 원격 Instagram 화면에서 로그인
   - 화면을 클릭해 입력칸 포커스
   - 아래 비밀번호형 입력칸에 내용을 입력
   - `선택한 곳에 입력` 클릭
   - Enter/Tab/2단계 인증도 화면을 보며 진행
4. `로그인 완료 확인`
5. 분석 사이트로 복귀
6. Instagram 게시글 URL 하나 입력 → `댓글 분석 시작`
7. 댓글은 기존 Google Sheet 아래쪽으로 append되며 기존 `comment_id`는 중복 저장하지 않음

## 보안

- Instagram 로그인 비밀번호/쿠키는 Vercel API에 저장하지 않습니다.
- 로그인 입력은 사용자의 브라우저에서 Worker로 직접 전송됩니다.
- Worker 프로필은 `PROFILE_ROOT/<client_id>`에 저장됩니다.
- `WORKER_API_KEY`를 GitHub에 커밋하지 마세요.
- Worker는 반드시 HTTPS로 배포하세요.
