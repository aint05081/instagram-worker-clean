# Instagram Browser Worker v10.6

로그인 UX/속도 개선 버전.

- Instagram 로그인 시 `/accounts/login/`으로 바로 이동
- 원격 브라우저가 먼저 뜨도록 `wait_until=commit` 사용
- font/media 리소스 차단으로 초기 로딩 경량화
- 모바일 키보드 포커스 유지 및 입력 API 재시도
- screenshot/status/click/type 요청 직렬화로 `요청 실패` 감소
- JPEG 저용량 스크린샷 사용
- 로그인 직후 Chromium 세션을 댓글 수집에 재사용 (재실행 제거)
- 30분 비활성 세션 자동 정리 (SESSION_IDLE_SECONDS로 변경 가능)

Railway에서는 기존 worker 폴더에 이 파일들을 덮어쓴 후 git push 하세요.
