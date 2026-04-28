# QFE AutoTC — 자동화 테스트 케이스 생성 파이프라인

Swagger(OpenAPI) 명세를 읽어 테스트 케이스를 자동 생성·실행·리포팅하는 파이프라인입니다.  
Rule 기반 결정론적 생성과 AI 보완 생성을 조합하며, GitHub Actions(self-hosted runner, Windows)로 1시간 주기 CI가 돌아갑니다.

---

## 목차

1. [프로젝트 구조](#프로젝트-구조)
2. [전체 실행 흐름](#전체-실행-흐름)
3. [모듈 상세](#모듈-상세)
4. [테스트 케이스 유형](#테스트-케이스-유형)
5. [Pass / Fail 판정 기준](#pass--fail-판정-기준)
6. [Experiment 모드 (Step 1 / 2 / 3)](#experiment-모드-step-1--2--3)
7. [설정 파일 (config.yaml)](#설정-파일-configyaml)
8. [GitHub Actions CI](#github-actions-ci)
9. [Windows Service (SCM) 운영](#windows-service-scm-운영)
10. [실행 방법](#실행-방법)
11. [주의사항](#주의사항)

---

## 프로젝트 구조

```
TestAgent/
├── main.py                          # 파이프라인 진입점 (CLI)
├── config/
│   └── config.yaml                  # 전체 파이프라인 설정
├── parsers/
│   ├── api_parser.py                # Swagger 2.0 파서 → 정규화된 endpoint 리스트
│   └── python_parser.py             # Python 모듈 함수 파서
├── agents/
│   ├── schema_enricher.py           # semantic tag + x_constraints 보강
│   ├── rule_based_generator.py      # Rule 기반 TC 생성 (결정론적, AI 없음)
│   ├── tc_generator.py              # 2-Layer 생성 오케스트레이터 (Rule + AI)
│   ├── duplicate_detector.py        # 의미 기반 중복 감지
│   ├── experiment_runner.py         # Step 1/2/3 실험 비교 실행기
│   └── llm_client.py                # AI 클라이언트 팩토리 (Ollama / Claude / Gemini)
├── tests/
│   ├── conftest.py                  # pytest 공용 fixture, crash 감지, diag JSONL 기록
│   ├── helpers/
│   │   ├── diag.py                  # 구조화된 진단 데이터 빌더 + 결과 분류
│   │   └── server_manager.py        # 서버 헬스체크 + 자동 재기동
│   ├── fixtures/
│   │   ├── face_320x240.jpg         # 정상 얼굴 이미지 픽스처
│   │   └── raw_b64_cache.py         # raw 이미지 Base64 캐시
│   ├── generated/
│   │   ├── rule/                    # Rule 기반 생성 TC
│   │   └── ai/                      # AI 보완 생성 TC
│   └── manual/                      # 수동 작성 TC
├── reports/
│   ├── excel_reporter.py            # pytest-json-report → Excel 변환
│   ├── excel_reporter2.py           # 실험 비교용 Excel 리포터
│   ├── summary.html                 # pytest-html 리포트
│   ├── pytest_report.json           # pytest-json-report 원본
│   └── test_diag.jsonl              # TC별 구조화 진단 로그 (JSONL)
├── runner/
│   └── test_runner.py               # pytest 실행 래퍼
├── watcher/
│   └── file_watcher.py              # Swagger 파일 변경 감지 (watchdog, WSL 대응)
├── notifier/
│   └── email_sender.py              # 결과 이메일 발송
├── scripts/
│   ├── service_install.ps1          # Windows Service 등록
│   ├── service_remove.ps1           # Windows Service 제거
│   ├── service_status.ps1           # 서비스 상태 조회
│   ├── on_service_failure.ps1       # 서비스 장애 시 실행 스크립트
│   ├── run_qfe_server_wrapper.ps1   # 서버 래퍼 (로그 보존 + 재기동)
│   ├── collect_crash_logs.ps1       # Crash 로그 수집
│   └── generate_excel_report.py     # 리포트 Excel 생성 CLI
├── .github/workflows/
│   ├── auto-tc.yml                  # 메인 CI (1시간 주기 + Swagger 변경 트리거)
│   └── crash-probe.yml              # Crash 유발 TC 격리 실험 워크플로
└── qfe-api-server-no-license/       # [절대 수정 금지] QFE 서버 바이너리 및 모델
```

---

## 전체 실행 흐름

```
Swagger 파일 (QFEapi.json)
        │
        ▼
[1] APIParser                         parsers/api_parser.py
    Swagger 2.0 → 정규화된 endpoint 리스트
    {path, method, operation_id, parameters, request_body, responses}
        │
        ▼
[2] SchemaEnricher                    agents/schema_enricher.py
    각 파라미터/필드에 semantic_tag + x_constraints 주입
    - semantic_tag: base64_image | threshold_numeric | numeric_id | boolean_flag | ...
    - x_constraints: {minimum, maximum, example, enum, ...}
        │
        ▼
[3] RuleBasedTCGenerator              agents/rule_based_generator.py
    결정론적 TC 코드 생성 (AI 없음)
    ├── positive           — 정상 케이스
    ├── missing_required   — 필수 필드 누락
    ├── wrong_type         — 잘못된 타입 전송
    ├── boundary           — min/max 경계값
    ├── input_validation   — semantic tag별 유효하지 않은 입력
    └── raw_image_relation — 이미지 크기·채널 불일치 케이스
        │
        ▼ (ai_augment.enabled = true 인 경우)
[4] LLM AI 보완                       agents/tc_generator.py + llm_client.py
    Rule 기반 TC를 컨텍스트로 제공 → 중복 없는 엣지 케이스만 생성
    중복 감지: duplicate_detector.py (함수명 + 의도 패턴 + 구조적 매칭)
        │
        ▼
[5] 파일 저장                         tests/generated/rule/ | ai/
    test_{operation_id}.py
    - 헤더 주석 (spec_hash, 재생성 방지용 fingerprint)
    - import 블록
    - Rule 기반 함수들
    - AI 보완 함수들 (있을 경우)
        │
        ▼
[6] TestRunner                        runner/test_runner.py
    pytest 실행
    --base-url, --alluredir, --html, --json-report
        │
        ▼
[7] 서버 크래시 감지 + 자동 재기동    main.py + conftest.py + server_manager.py
    - 각 TC 실행 후 서버 헬스체크
    - 크래시 감지 시: diag에 server_crash=True + 로그 tail 주입
    - 재기동 후 남은 TC만 재실행 (passed TC 건너뜀)
        │
        ▼
[8] 결과 수집 + Excel 리포트          reports/excel_reporter.py
    pytest_report.json + test_diag.jsonl → test_report.xlsx
    컬럼: 엔드포인트, TC 유형, 실행 결과, Failure Cause, match_score, ...
        │
        ▼
[9] 이메일 발송                       notifier/email_sender.py
    요약 + HTML 리포트 첨부
```

---

## 모듈 상세

### parsers/api_parser.py

Swagger 2.0 JSON/YAML 명세를 읽어 endpoint 리스트로 정규화합니다.

- URL 또는 로컬 파일 경로 모두 지원
- `parameters` (path / query / header) + `request_body` 분리 정규화
- `$ref` 참조 inline 해석
- 응답 스키마(`responses`) 수집

출력 shape:
```python
{
  "path": "/api/v2/user/{user_id}/enroll",
  "method": "post",
  "operation_id": "post_/api/v2/user/{user_id}/enroll",
  "parameters": [...],   # path/query params
  "request_body": {...}, # body schema
  "responses": {...}
}
```

---

### agents/schema_enricher.py

파싱된 각 파라미터/필드 스키마에 두 가지 정보를 보강(enrich)합니다.

**semantic_tag** — 필드의 의미적 역할을 분류:

| 태그 | 의미 |
|------|------|
| `base64_image` | Base64 인코딩된 이미지 |
| `base64_template` | Base64 인코딩된 얼굴 템플릿 |
| `threshold_numeric` | 매칭 임계값 (0.0~1.0 또는 정수 범위) |
| `numeric_id` | 정수형 ID |
| `path_user_id` | 경로 파라미터 user_id |
| `boolean_flag` | 불리언 플래그 |
| `channel_count` | 이미지 채널 수 |
| `plain_string` | 일반 문자열 |

**x_constraints** — 제약 조건 보강:
```python
schema["x_constraints"] = {
  "minimum": 0,
  "maximum": 1,
  "example": 0.7,
  "enum": [...],
}
```

결과는 `.cache/semantic_tags.json`에 캐싱됩니다.

---

### agents/rule_based_generator.py

결정론적 TC 코드를 생성하는 핵심 모듈입니다. AI를 사용하지 않으며 동일한 입력에 동일한 출력을 보장합니다.

**생성 규칙별 동작:**

| 규칙 | 생성 케이스 |
|------|-------------|
| `positive` | Swagger example / semantic tag별 유효값으로 구성된 정상 요청 |
| `missing_required` | 필수 파라미터/필드 하나씩 누락 |
| `wrong_type` | 정수 자리에 문자열, 문자열 자리에 정수 등 |
| `boundary` | min, min-1, max, max+1 경계값 (정수 계열 float → int 자동 변환) |
| `input_validation` | base64 빈값/잘못된값, threshold 범위 초과, numeric_id 음수/0 등 |
| `raw_image_relation` | width×height×channel vs image_data 크기 불일치 |

**`_good_value()` 우선순위:**
1. `x_constraints.example` (schema_enricher가 저장한 값)
2. `schema.get("example")`
3. min/max 중간값
4. semantic tag별 기본값

**`_range_cases()` 정수 강제 변환:**  
Go 서버의 JSON unmarshal은 타입에 엄격하므로, `50000.0` 같은 정수 계열 float를 `50000`으로 자동 변환합니다.

---

### agents/tc_generator.py

Rule 기반 + AI 보완 2-레이어 생성을 오케스트레이션합니다.

- **fingerprint 기반 dedup**: `operation_id + method + path + params` 해시로 이미 생성된 파일 재생성 방지
- **AST 기반 검증**: AI 생성 코드를 `ast.parse()` + `pytest --collect-only`로 검증 후 저장
- **레이어 병합**: Rule 함수 블록 + AI 함수 블록을 단일 `.py` 파일로 합산

---

### agents/duplicate_detector.py

Rule 기반 TC와 AI 생성 TC 간 의미적 중복을 감지합니다.

감지 전략 (우선순위):
1. 함수명 완전 일치 → 확정 중복
2. 의도 패턴 매칭 → `(op_id, intent_type, target_field)` 정규화 후 비교
3. 구조적 매칭 → 호출 파라미터 오버랩 휴리스틱

```
test_getUserById_missing_id      →  IntentKey("getUserById", "missing_required", "id")
test_getUserById_wrong_type_age  →  IntentKey("getUserById", "wrong_type", "age")
```

---

### tests/conftest.py

pytest 공용 설정 및 서버 크래시 감지를 담당합니다.

- `--base-url` CLI 옵션 등록
- 각 TC의 `call` 단계 완료 후 서버 헬스체크
- 크래시 감지 시: `diag`에 `server_crash=True` + 로그 tail 주입
- `teardown` 단계에서 자동 재기동 시도
- 모든 TC(passed/failed 불문)의 `diag`를 `reports/test_diag.jsonl`에 기록

---

### tests/helpers/diag.py

TC 실행 결과의 구조화된 진단 데이터를 빌드합니다.

**axis (실패 축):**

| axis | 의미 |
|------|------|
| `schema` | 타입/필수/포맷 위반 |
| `domain` | 범위/enum/base64/이미지 관계 위반 |
| `state` | DB/사용자/템플릿 상태 의존 |
| `runtime` | 서버 crash/연결 거부/타임아웃 |

**`classify_result()`** — pytest PASS/FAIL 이후 Excel 컬럼용 레이블 생성 (pass/fail 판정 자체에는 영향 없음)

---

### reports/excel_reporter.py

`pytest_report.json` + `test_diag.jsonl`을 읽어 Excel 리포트를 생성합니다.

주요 컬럼:

| 컬럼 | 설명 |
|------|------|
| 엔드포인트, 메서드 | API 경로 및 HTTP 메서드 |
| TC 유형 | positive / missing_required / wrong_type / boundary / ... |
| Expected HTTP Status | 기대 HTTP 상태 코드 |
| Actual HTTP Status | 실제 응답 코드 |
| HTTP Match | 일치 여부 |
| Expected Error Code(s) | 참고용 — pass/fail 판정 미사용 |
| Actual Error Code | 참고용 — pass/fail 판정 미사용 |
| Failure Level | HTTP_STATUS_MISMATCH / BODY_SCHEMA_MISMATCH / PASS / ... |
| Failure Cause | 원인 분류 레이블 (pass/fail 판정과 무관) |
| match_score | match 엔드포인트 전용, 항상 기록 |

---

### watcher/file_watcher.py

Swagger 파일 변경을 감지해 파이프라인을 자동 트리거합니다.

- watchdog 기반, WSL 환경에서 `PollingObserver`로 자동 전환
- 큐 기반 처리 (동시 실행 방지, debounce coalesce)
- 동일 파일 짧은 반복 이벤트 → 마지막 이벤트만 처리

---

## 테스트 케이스 유형

| 유형 | 설명 | 예시 |
|------|------|------|
| `positive` | 정상 요청 | 유효한 user_id + 정상 이미지 |
| `missing_required` | 필수 필드 누락 | body에서 `image_data` 제거 |
| `wrong_type` | 잘못된 타입 | `user_id`에 문자열 전송 |
| `boundary` | 경계값 | threshold=0, threshold=-1, threshold=1 |
| `input_validation` | 의미적 유효성 | base64 빈 문자열, 음수 numeric_id |
| `raw_image_relation` | 이미지 크기 불일치 | width×height×channel ≠ image_data 바이트 수 |

---

## Pass / Fail 판정 기준

### http_status 모드 (현재 운영 모드)

| 엔드포인트 유형 | Pass 조건 |
|----------------|-----------|
| 일반 positive | `HTTP 200` + `success == True` |
| 일반 negative | `HTTP 4xx` + `success == False` |
| 상태 의존 엔드포인트 | `HTTP 200` (success=True) 또는 `HTTP 404` (success=False) |
| match 엔드포인트 | `HTTP [200, 404]` + `success True/False` + `match_status` 일치 |

**error_code는 pass/fail 판정에 사용하지 않습니다.**  
error_code는 `test_diag.jsonl` 및 Excel의 `Failure Cause` 컬럼에 기록되어 실패 원인 분류에만 활용됩니다.

**match_score**는 테스트 결과(pass/fail)와 무관하게 항상 `diag`에 기록됩니다.

---

## Experiment 모드 (Step 1 / 2 / 3)

`CLAUDE.md` 규칙에 따른 3단계 실험 비교 모드입니다.

| Step | 설명 | 중복 처리 |
|------|------|-----------|
| Step 1 | AI 단독 TC 생성 (Rule 없음) | — |
| Step 2 | Rule + AI 동시 생성, 중복 허용 | 중복 개수 리포트 |
| Step 3 | Rule + AI 동시 생성, AI 중복 제거 | AI TC에서 Rule과 중복되는 것 제외 |

출력 레이아웃:
```
tests/generated/
  step{N}/
    {provider}/
      rule/   test_{op_id}.py
      ai/     test_{op_id}.py
```

리포트: `reports/experiment_report.json`, `reports/experiment_tc_report.csv`

---

## 설정 파일 (config.yaml)

```yaml
server:
  base_url: "http://127.0.0.1:8080"
  error_response_mode: http_status   # qfe | http_status | hybrid
  match_threshold: 0

tc_generation:
  rule_based:
    enabled: true
    include:
      - positive
      - missing_required
      - wrong_type
      - boundary
      - input_validation
      - raw_image_relation
  ai_augment:
    enabled: false    # true 시 LLM 보완 생성 활성화

agent:
  provider: ollama    # ollama | claude | gemini
  model: llama3.1:8b

runner:
  test_dirs:
    - "./tests/generated/rule"
    - "./tests/generated/ai"
    - "./tests/manual"
  timeout_seconds: 90
```

**error_response_mode 값:**

| 값 | 의미 |
|----|------|
| `qfe` | 서버가 항상 HTTP 200 반환, success/error_code로 판단 |
| `http_status` | RESTful HTTP 상태코드 (400/404/422/500) 기반 |
| `hybrid` | 두 방식 혼용 |

---

## GitHub Actions CI

파일: `.github/workflows/auto-tc.yml`

**트리거:**
- 1시간 주기 (cron: `0 * * * *`)
- `QFEapi.json` 또는 workflow 파일 변경 시 자동 실행
- 수동 트리거 (`workflow_dispatch`)

**변경 감지 로직:**
```
job1: detect-changes
  - 서버 버전 + Swagger 파일 해시 조회
  - 이전 실행과 비교 → 변경 없으면 skip

job2: run-tests (should_run == true 인 경우만)
  - pip 의존성 설치
  - TC 생성 (python main.py generate ...)
  - 테스트 실행 (python main.py run ...)
  - Excel 리포트 생성
  - 결과 아티팩트 업로드
```

**workflow_dispatch 파라미터:**

| 파라미터 | 설명 |
|----------|------|
| `server_target` | local / external 서버 선택 |
| `base_url` | 테스트 대상 서버 URL |
| `error_response_mode` | qfe / http_status / hybrid |
| `match_threshold` | match/verify score 임계값 |
| `reset_semantic_cache` | semantic cache 강제 초기화 |
| `run_crash_probe` | Crash Probe job 강제 실행 |

---

## Windows Service (SCM) 운영

QFE 서버를 Windows Service로 등록하여 자동 재시작을 보장합니다.

**서비스 스크립트:**

| 스크립트 | 역할 |
|----------|------|
| `scripts/service_install.ps1` | sc.exe로 서비스 등록 + failure action 설정 |
| `scripts/service_remove.ps1` | 서비스 제거 |
| `scripts/service_status.ps1` | 서비스 상태 조회 |
| `scripts/on_service_failure.ps1` | 서비스 장애 시 실행 — 로그 보존 후 재시작 |
| `scripts/run_qfe_server_wrapper.ps1` | 서버 프로세스 래퍼 — stderr 로그 파일 저장 |
| `scripts/collect_crash_logs.ps1` | Windows Event Log + 파일 로그 수집 |

**sc_failure 설정 원칙:**
- 1차 실패 → restart (자동 재시작)
- 2차 실패 → run (on_service_failure.ps1 실행)
- 3차 실패 → reboot (선택적)
- 장애 이력 초기화 주기: 86400초(1일)

---

## 실행 방법

```powershell
# 의존성 설치
pip install -r requirements.txt

# 파일 감시 모드 (기본)
python main.py watch

# Swagger 파일 지정 실행 (파싱 → TC 생성 → 테스트 → 이메일)
python main.py run QFEapi.json

# TC 생성만
python main.py generate QFEapi.json

# Swagger 파싱 결과 확인
python main.py parse QFEapi.json

# Step 1/2/3 실험 비교
python main.py experiment QFEapi.json
```

**테스트 단독 실행:**
```powershell
pytest tests/generated/rule/ --base-url=http://127.0.0.1:8080 -v
```

**Excel 리포트 재생성:**
```powershell
python scripts/generate_excel_report.py
```

---

## 주의사항

- `qfe-api-server-no-license/` 폴더 아래는 **절대 수정 금지**
- `error_response_mode`를 변경하면 TC를 반드시 재생성해야 합니다 (`python main.py generate ...`)
- Semantic cache를 초기화하려면 `config.yaml`에서 `tc_generation.semantic_tagging.reset_cache: true`로 설정하세요
- WSL 환경에서 파일 감시 시 NTFS 마운트의 inotify 한계로 `PollingObserver`가 자동 사용됩니다
- 생성된 TC 파일을 수동 수정한 경우, 다음 파이프라인 실행 시 fingerprint 검사로 재생성이 건너뛰어질 수 있습니다
