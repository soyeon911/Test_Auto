[GitHub Actions]
요구사항
1. 1시간 주기 실행
2. swagger 업로드/변경 시 자동 트리거
3. 서버 commit/hash/version과 swagger hash/version 비교 후 변경 시만 실행
4. Windows 기반 환경
5. 서버 crash 후 로그 보존 + 재기동 후 재개

[SCM(Service Control Manager)]
1. Windows Service 로 운영
2. 서버가 죽었을 때 그 상황 기록 후 재실행하는 스크립트 제작
3. sc_failure로 restart/run/reboot 를 통해 서비스 자동 재시작, 실패 시 특정 명령 실행, 서비스 상태 조회
4. sc.exe 또는 서비스 설정을 통해 event log + file log 생성