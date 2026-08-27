# v10.4

- Instagram 원격 로그인 화면 모바일 최적화
- 휴대폰 접속 시 원격 Chromium을 390x844 모바일 뷰포트 + 터치 모드로 실행
- 로그인 페이지를 모바일 1열 레이아웃, sticky 하단 버튼, safe-area 대응으로 개선
- 터치 좌표를 실제 원격 브라우저 viewport 기준으로 계산
- 데스크톱 접속 시 기존 1280x900 레이아웃 유지

Railway optional variables:
- MOBILE_VIEWPORT_W=390
- MOBILE_VIEWPORT_H=844
