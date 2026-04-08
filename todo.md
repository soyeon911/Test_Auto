다만 무료 버전에서 실제로 자주 맞닥뜨릴 문제는 여전히 있다:
	• 응답 형식 흔들림 
	• 너무 일반적인 테스트 생성 
	• spec 해석 한계 
	• collect 단계 실패
	TODO 2. AI 생성 코드 검증에 pytest --collect-only 추가
	문제
	지금은 ast.parse()만 통과하면 저장한다.
	그런데 실제로는 문법은 맞아도:
		• import 오류 
		• fixture 이름 오류 
		• parametrize 구조 오류 
		• pytest 수집 실패 
	가 자주 난다.
	해야 할 일
	AI 생성 코드에 대해:
		1. ast.parse() 
		2. 임시 파일 저장 
		3. pytest --collect-only temp_file 
	검증 후 통과한 코드만 반영
	이유
	Gemini 무료 버전은 출력 포맷 흔들림이 있어서 이 검증이 특히 중요하다.
	
	TODO 3. OpenAPI path-level parameters 병합
	문제
	지금 parser가 operation-level parameters만 읽으면,
	path-level parameter를 놓칠 수 있다.
	예:
	
	/users/{id}:
	  parameters:
	    - name: id
	      in: path
	해야 할 일
		• paths[path] 레벨의 parameters와 
		• get/post/... operation 레벨 parameters를
merge해서 parse 
	이유
	이거 빠지면 generated test payload가 틀어질 수 있다.
	
	TODO 4. append 로직에서 decorator 보존
	문제
	현재 새 테스트 함수 추출이 def test_... 기준이면,
	@pytest.mark.parametrize, @pytest.mark.xfail 같은 decorator가 잘릴 수 있다.
	해야 할 일
		• 함수 추출 시 decorator 줄까지 포함 
		• 가능하면 regex보다 ast 또는 source block 기반으로 추출 
	이유
	지금은 단순 테스트만 생성해도, 나중에 mark/parametrize 붙는 순간 깨진다.
	
	TODO 5. watcher 실행을 queue 기반으로 바꾸기
	문제
	watchdog 이벤트가 짧은 시간에 여러 번 들어오면:
		• 중복 실행 
		• 동시 실행 
		• 파일 append 충돌
이 날 수 있다. 
	해야 할 일
		• watcher는 이벤트를 queue에 넣기만 함 
		• worker 1개가 순차적으로 pipeline 처리 
		• 동일 파일 짧은 시간 반복 이벤트는 debounce + coalesce 
	이유
	“자동화”라고 부르려면 가장 먼저 안정적으로 한 번씩만 돌아야 한다.
	
	2. 높은 우선순위: PoC를 MVP로 올리는 작업
	TODO 6. generated 파일 정책을 append → overwrite 중심으로 바꾸기
	문제
	append 방식은 초기엔 편하지만 시간이 지나면:
		• 오래된 테스트 잔존 
		• spec 변경 후 예전 테스트 남음 
		• generated 파일 비대화 
		• 관리 어려움 
	해야 할 일
	자동 생성 파일은:
		• endpoint별 파일 1개 
		• 재생성 시 전체 overwrite 
		• manual test는 별도 유지 
	예:
		• tests/generated/test_users_get.py 
		• tests/manual/test_users_get_manual.py 
	이유
	자동 생성 영역은 “기계가 다시 만들 수 있는 파일”이어야 한다.
	
	TODO 7. rule-based generator를 더 강하게 만들기
	문제
	지금 AI augmentation 구조는 좋아졌지만,
	기본 deterministic 테스트가 약하면 무료 LLM 의존도가 다시 커진다.
	해야 할 일
	OpenAPI 기준 최소한 아래는 rule-based로 보장:
		• 정상 요청 1개 
		• required 누락 
		• 잘못된 타입 
		• enum invalid 
		• 숫자 min/max boundary 
		• path/query/header/body 분리 케이스 
		• 인증 누락(해당 시) 
	이유
	무료 Gemini는 “엣지 케이스 보조 생성기” 정도로만 쓰는 게 안정적이다.
	
	TODO 8. spec 변경 감지(diff/hash) 추가
	문제
	지금은 input 들어오면 다시 생성하는 성격이 강하다.
	장기적으로는 변경된 endpoint만 반영해야 한다.
	해야 할 일
		• spec 파일 hash 저장 
		• endpoint별 fingerprint 저장 
		• added / modified / removed 판별 
	이유
	이게 있어야 “TC 추가 자동화”가 운영 가능해진다.
	
	TODO 9. parser의 schema 해석 강화
	문제
	복잡한 OpenAPI에서 아래가 나오면 현재 구조가 흔들릴 수 있다.
		• nested $ref 
		• allOf 
		• oneOf 
		• anyOf 
		• array 내부 object ref 
	해야 할 일
	우선순위는 이렇게:
		1. nested $ref 
		2. array item $ref 
		3. allOf 
		4. oneOf/anyOf 
	이유
	테스트 품질의 상한은 parser 품질이 결정한다.
	
	TODO 10. 실패 유형을 구분해서 report에 반영
	문제
	지금은 실행 결과 중심이면,
	“왜 실패했는지”가 운영자 입장에서 अस्पष्ट해질 수 있다.
	해야 할 일
	리포트에서 최소 구분:
		• parse 실패 
		• generate 실패 
		• syntax 실패 
		• collect 실패 
		• runtime test 실패 
	이유
	AI 자동화 시스템은 “생성 실패”와 “테스트 실패”를 분리해서 봐야 한다.
	
	3. 중간 우선순위: 운영 편의성 개선
	TODO 11. pipeline 실행 조건 분기
	문제
	새 테스트가 하나도 안 생겼는데도 pytest full run을 계속 돌리면 비효율적이다.
	해야 할 일
		• 변경/생성된 TC가 있으면 full run 
		• 없으면 skip 또는 smoke run 
		• config로 제어 가능하게 
	이유
	자동화는 오래 돌릴수록 불필요한 실행을 줄이는 게 중요하다.
	
	TODO 12. 메일 발송 조건 강화
	문제
	매번 메일 쏘면 곧 스팸처럼 느껴진다.
	해야 할 일
	설정 가능하게:
		• 실패 시만 메일 
		• 신규 TC 생긴 경우만 메일 
		• 하루 1회 summary 
		• 특정 수신자 그룹 분기 
	이유
	리포트보다 중요한 건 “누가 언제 알림을 받는가”다.
	
	TODO 13. 로그/아티팩트 구조 정리
	해야 할 일
	실행마다 아래를 저장:
		• 입력 파일명 
		• 파싱 요약 
		• 생성된 endpoint 수 
		• 생성된 test 수 
		• collect 결과 
		• pytest 결과 
		• report 경로 
		• mail 발송 여부 
	예:
	
	runs/
	  2026-04-08_110500/
	    input_meta.json
	    parsed_endpoints.json
	    generated_files.json
	    collect.log
	    pytest.log
	    summary.json
	이유
	나중에 디버깅할 때 엄청 중요하다.
	
	TODO 14. config validation 추가
	문제
	config 값 누락 시 런타임 중간에 터질 수 있다.
	해야 할 일
	시작 시:
		• 필수 키 체크 
		• provider별 필요한 env 체크 
		• report path 존재 여부 체크 
		• input dir 존재 여부 체크 
	이유
	운영 도중보다 시작 시 빨리 죽는 게 낫다.
	
	4. 나중에 고도화할 것
	TODO 15. Python 함수 / library metadata 입력 확장
	지금은 OpenAPI 중심으로 가는 게 맞다.
	하지만 네 최종 목표가 “agent 함수나 library input 기반 테스트 자동화”까지 포함이면 나중엔 이것도 필요하다.
	해야 할 일
		• inspect.signature() 기반 Python 함수 parser 
		• docstring 기반 설명 추출 
		• wrapper metadata 기반 C++ 라이브러리 테스트 입력 정규화 
	
	TODO 16. 테스트 데이터 전략 추가
	지금은 구조 위주지만, 나중엔 실제 값 품질도 중요해진다.
	예:
		• 문자열 길이 boundary 
		• UUID 형식 
		• 이메일 형식 
		• 날짜 형식 
		• 파일 업로드 샘플 
		• 인증 토큰 fixture 
	
	TODO 17. CI/CD 연동 고도화
	나중에 붙이면 좋은 것:
		• GitHub Actions + self-hosted runner 
		• Jenkins pipeline 
		• changed spec만 실행 
		• artifact 업로드 
		• report link 공유 
	
	TODO 18. quarantine/review 단계 추가
	AI가 생성한 테스트를 바로 반영하지 않고:
		• collect 실패 시 quarantine 
		• flaky test는 review 필요 
		• low-confidence output은 별도 폴더 
	이런 단계가 있으면 품질 관리가 좋아진다.
	
	5. 추천 우선순위 로드맵
	1차
	반드시 바로 수정
		1. dedup hash 통일 
		2. collect-only 검증 추가 
		3. path-level parameters 병합 
		4. decorator 보존 
		5. queue 기반 watcher 
	2차
	MVP 안정화
	6. generated overwrite 정책
	7. rule-based generator 강화
	8. spec diff/hash 추가
	9. failure type report 분리
	10. artifact/log 정리
	3차
	운영 고도화
	11. 실행 조건 분기
	12. 메일 정책 세분화
	13. config validation
	14. schema 해석 확장
	4차
	확장 기능
	15. python/library parser
	16. richer test data
	17. CI/CD 고도화
	18. quarantine/review workflow
