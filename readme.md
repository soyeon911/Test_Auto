# AutoTC — Automated Test Case Generator

OpenAPI 명세(또는 Python 모듈)를 입력받아 **테스트 케이스를 자동으로 생성·실행하고 결과를 이메일로 발송**하는 자동화 파이프라인입니다.

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [전체 아키텍처](#2-전체-아키텍처)
3. [파이프라인 흐름](#3-파이프라인-흐름)
4. [모듈별 코드 설명](#4-모듈별-코드-설명)
   - [main.py — 진입점 & CLI](#mainpy--진입점--cli)
   - [app.py — 테스트 대상 FastAPI 서버](#apppy--테스트-대상-fastapi-서버)
   - [parsers/ — 입력 파싱](#parsers--입력-파싱)
   - [agents/ — 테스트 케이스 생성](#agents--테스트-케이스-생성)
   - [runner/ — 테스트 실행](#runner--테스트-실행)
   - [notifier/ — 이메일 발송](#notifier--이메일-발송)
   - [tests/ — 테스트 픽스처](#tests--테스트-픽스처)
5. [설정 파일 (config.yaml)](#5-설정-파일-configyaml)
6. [사용 방법](#6-사용-방법)
7. [디렉토리 구조](#7-디렉토리-구조)
8. [의존성](#8-의존성)

---

## 1. 프로젝트 개요

AutoTC는 다음 두 가지 소스를 입력으로 받아 pytest 테스트 코드를 자동 생성합니다.

- **OpenAPI/Swagger 명세** (YAML, JSON, 또는 URL)
- **Python 모듈/파일** (함수 시그니처 기반)

생성된 테스트는 **두 개의 레이어**로 구성됩니다.

| 레이어 | 방식 | 역할 |
|--------|------|------|
| Layer 1 | 규칙 기반 (Rule-Based) | 결정론적 케이스 (happy-path, 필수 필드 누락, 타입 오류 등) |
| Layer 2 | AI 보강 (LLM) | 엣지 케이스 (도메인 특화, 조합 오류, SQL Injection 프로브 등) |

---

## 2. 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                          main.py                            │
│   (watch / run / parse / generate 모드 CLI 진입점)           │
└────────────────────────┬────────────────────────────────────┘
                         │
           ┌─────────────▼─────────────┐
           │   detect_source_and_parse  │  ← 소스 자동 감지
           └─────────────┬─────────────┘
                         │
           ┌─────────────▼─────────────┐
           │       parsers/             │
           │  ┌───────────────────────┐ │
           │  │  OpenAPIParser        │ │  ← YAML/JSON/URL 파싱
           │  │  PythonFunctionParser │ │  ← Python 모듈 파싱
           │  └───────────────────────┘ │
           └─────────────┬─────────────┘
                         │  endpoint 목록
           ┌─────────────▼─────────────┐
           │       agents/              │
           │  ┌───────────────────────┐ │
           │  │  TCGeneratorAgent     │ │  ← 오케스트레이터
           │  │  ├─ RuleBasedTCGen.  │ │  ← Layer 1: 규칙 기반
           │  │  └─ LLM Client       │ │  ← Layer 2: AI 보강
           │  └───────────────────────┘ │
           └─────────────┬─────────────┘
                         │  tests/generated/*.py
           ┌─────────────▼─────────────┐
           │       runner/              │
           │     TestRunner             │  ← pytest 실행 + 리포트
           └─────────────┬─────────────┘
                         │  summary dict
           ┌─────────────▼─────────────┐
           │       notifier/            │
           │     EmailSender            │  ← HTML 이메일 발송
           └───────────────────────────┘
```

---

## 3. 파이프라인 흐름

`run` 모드를 기준으로 한 번의 실행 흐름입니다.

```
1. 설정 로드     config/config.yaml 읽기
       ↓
2. 소스 파싱     OpenAPI 파일 또는 Python 모듈 → endpoint 목록 추출
       ↓
3. TC 생성       각 endpoint에 대해:
                 ├─ [Layer 1] 규칙 기반 pytest 함수 생성
                 └─ [Layer 2] LLM에 전달 → 엣지 케이스 함수 추가
                 → tests/generated/test_<operation_id>.py 저장
       ↓
4. 테스트 실행   pytest 서브프로세스 실행
                 → allure-results/, summary.html, pytest_report.json 생성
       ↓
5. 이메일 발송   HTML 본문 + summary.html 첨부 → SMTP 전송
```

---

## 4. 모듈별 코드 설명

### `main.py` — 진입점 & CLI

**역할:** argparse 기반 CLI. 4가지 실행 모드를 제공합니다.

| 모드 | 동작 |
|------|------|
| `watch` (기본) | `input/` 폴더를 감시하다가 새 API 명세 파일이 들어오면 자동으로 전체 파이프라인 실행 |
| `run <file>` | 지정한 소스에 대해 파싱 → TC 생성 → 테스트 실행 → 이메일까지 전체 파이프라인 1회 실행 |
| `parse <file>` | 파싱 결과만 콘솔에 출력 (TC 생성 없음) |
| `generate <file>` | 파싱 + TC 생성까지만 수행 (테스트 실행 없음) |

**핵심 함수 `detect_source_and_parse()`:**  
파일 확장자와 URL 여부를 확인해 소스 타입을 자동으로 판별합니다.

```python
if source.startswith(("http://", "https://")):   # URL → OpenAPIParser
elif p.suffix in {".yaml", ".yml", ".json"}:      # 파일 → OpenAPIParser
elif p.suffix == ".py" or not p.suffix:           # Python → PythonFunctionParser
```

---

### `app.py` — 테스트 대상 FastAPI 서버

**역할:** AutoTC가 테스트할 샘플 REST API 서버입니다. 인메모리 딕셔너리를 DB로 사용합니다.

| 엔드포인트 | 메서드 | 동작 |
|------------|--------|------|
| `/users` | GET | 전체 사용자 목록 반환 (`limit` 파라미터 지원) |
| `/users` | POST | 새 사용자 생성 (name, email 필수) → 201 반환 |
| `/users/{user_id}` | GET | 특정 사용자 조회, 없으면 404 |

---

### `parsers/` — 입력 파싱

#### `openapi_parser.py` — OpenAPI/Swagger 파서

OpenAPI 3.x / Swagger 2.x 명세를 읽어 **표준화된 endpoint 딕셔너리 목록**으로 변환합니다.

**처리 흐름:**
```
load()  →  파일 읽기 또는 URL fetch (YAML/JSON 자동 판별)
parse() →  paths 순회 → 각 HTTP 메서드별 _parse_operation() 호출
```

`_parse_operation()`이 반환하는 endpoint 딕셔너리 구조:
```python
{
    "path": "/users/{id}",
    "method": "get",
    "operation_id": "getUser",
    "summary": "...",
    "parameters": [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ],
    "request_body": None | {"content_type": "application/json", "schema": {...}},
    "responses": {
        "200": {"description": "OK", "schema": {...}},
        "404": {"description": "Not found", "schema": None}
    }
}
```

`$ref` 참조는 `_resolve_ref()`가 재귀적으로 해석합니다.

---

#### `python_parser.py` — Python 함수 파서

Python 모듈의 함수 시그니처를 `inspect` 모듈로 분석해 OpenAPIParser와 **동일한 형태의 endpoint 딕셔너리**로 변환합니다. TC 생성 로직이 소스 타입에 관계없이 동일하게 동작할 수 있도록 합니다.

- `load()`: 점(`.`) 표기 모듈명 또는 `.py` 파일 경로 모두 지원
- `parse()`: `_` 로 시작하지 않는 모든 public 함수를 파싱
- 타입 힌트(`int`, `str`, `bool` 등)를 JSON Schema 타입(`integer`, `string`, `boolean`)으로 변환
- 기본값 없는 파라미터 → `required: True`

---

### `agents/` — 테스트 케이스 생성

#### `tc_generator.py` — 오케스트레이터

**역할:** Layer 1(규칙 기반)과 Layer 2(AI)를 조율해 최종 pytest 파일을 생성합니다.

**주요 처리 로직:**

1. **중복 방지 (Dedup):** 생성된 코드 전체를 SHA-256 해시하여 기존 파일들의 해시와 비교합니다. 동일 해시가 있으면 생성을 건너뜁니다.

2. **파일 쓰기 전략:**
   - 새 파일: 헤더(import, 메타정보) + Layer 1 + Layer 2 순서로 작성
   - 기존 파일: `_only_new_functions()`로 이미 존재하는 함수명을 필터링한 뒤 **추가(append)만** 수행 → 수동 편집 내용을 덮어쓰지 않음

3. **AI 생성 재시도:** LLM 응답이 유효한 Python 문법이 아니면 최대 3회까지 재시도하며, 이전 시도의 오류를 프롬프트에 포함시킵니다.

**AI 프롬프트 전략:**  
규칙 기반으로 이미 생성된 테스트 코드를 AI에게 함께 전달합니다. 이를 통해 AI가 **이미 커버된 케이스를 중복 생성하지 않고** 순수한 엣지 케이스에 집중하도록 유도합니다.

---

#### `rule_based_generator.py` — 규칙 기반 TC 생성기

결정론적으로 5가지 유형의 pytest 함수를 생성합니다.

| 규칙 | 생성 내용 | 예상 응답 코드 |
|------|-----------|---------------|
| `positive` | 모든 필수 파라미터를 올바른 타입으로 채운 정상 요청 | 2xx |
| `missing_required` | 필수 파라미터/바디 필드를 하나씩 빼고 요청 | 400 / 422 |
| `wrong_type` | 파라미터에 잘못된 타입 전달 (integer에 문자열 등) | 400 / 422 |
| `boundary` | 정수 파라미터에 `0`, `-1`, `2,147,483,647` 값 전달 | 5xx 아니어야 함 |
| `invalid_enum` | enum 필드에 허용되지 않는 값 전달 | 400 / 422 |

각 규칙은 `config.yaml`의 `tc_generation.rule_based.include` 목록으로 개별 활성화/비활성화할 수 있습니다.

`_render_call()`이 path params, query params, request body를 조합해 `requests.<method>(...)` 호출 코드를 문자열로 렌더링합니다.

---

#### `llm_client.py` — LLM 클라이언트 팩토리

3가지 AI 제공자를 추상화합니다. 모두 `BaseLLMClient`를 상속하며 `generate(system_prompt, user_prompt) → str` 인터페이스를 구현합니다.

| 제공자 | 클래스 | 필요 환경변수 |
|--------|--------|--------------|
| Google Gemini (기본) | `GeminiClient` | `GEMINI_API_KEY` |
| Anthropic Claude | `AnthropicClient` | `ANTHROPIC_API_KEY` |
| OpenAI | `OpenAIClient` | `OPENAI_API_KEY` |

`create_llm_client(config)`가 팩토리 함수로, config의 `agent.provider` 값을 읽어 알맞은 클라이언트 인스턴스를 반환합니다. API 키는 환경변수에서만 읽으며 코드에 하드코딩하지 않습니다.

---

### `runner/` — 테스트 실행

#### `test_runner.py` — pytest 실행기

**역할:** pytest를 서브프로세스로 실행하고 결과를 수집합니다.

**pytest 실행 커맨드 구성:**
```
pytest ./tests/generated ./tests/manual
  --base-url=<서버URL>
  --alluredir=./reports/allure-results
  --html=./reports/summary.html
  --json-report --json-report-file=./reports/pytest_report.json
  --tb=short -q
```

**결과 수집:**  
`pytest_report.json`을 파싱해 `passed`, `failed`, `total`, `failed_tests` (실패한 테스트의 nodeid와 오류 내용)을 포함한 summary 딕셔너리를 반환합니다.

**Allure 리포트:**  
`allure` CLI가 설치된 경우 자동으로 HTML 리포트도 생성합니다. 없으면 경고만 출력하고 계속 진행합니다.

---

### `notifier/` — 이메일 발송

#### `email_sender.py` — Gmail SMTP 발송기

**역할:** 테스트 결과 요약을 HTML 이메일로 발송합니다.

**이메일 구성:**
- **제목:** `✅ TC Report — 5✓ 0✗` 또는 `❌ TC Report — 3✓ 2✗` 형식 (pass/fail 수 포함)
- **본문:** 인라인 CSS가 적용된 HTML 테이블 (총 실행 수, 통과/실패 수, 소요 시간, 실패 테스트 목록)
- **첨부파일:** `summary.html` (config의 `attach_report: true` 시)

SMTP 비밀번호는 절대 설정 파일에 저장하지 않고 환경변수(`SMTP_PASSWORD`)에서만 읽습니다.

---

### `tests/` — 테스트 픽스처

#### `conftest.py` — 공용 pytest 픽스처

모든 테스트 파일에서 공유하는 픽스처를 정의합니다.

| 픽스처 | 범위 | 역할 |
|--------|------|------|
| `base_url` | session | `--base-url` CLI 옵션 또는 `BASE_URL` 환경변수로 서버 주소 주입 |
| `http_session` | session | `base_url`이 설정된 `requests.Session` 재사용 객체 |
| `assert_server_running` | function | `/health` 엔드포인트로 서버 상태 확인, 연결 불가 시 테스트 skip |

생성된 모든 테스트 함수는 `base_url`을 첫 번째 인자로 받아 이 픽스처를 통해 서버 주소를 주입받습니다.

---

## 5. 설정 파일 (`config.yaml`)

```yaml
server:
  base_url: "http://localhost:8000"    # 테스트 대상 서버

agent:
  provider: gemini                      # gemini | anthropic | openai
  model: gemini-2.0-flash
  api_key_env: GEMINI_API_KEY           # API 키를 담은 환경변수명

tc_generation:
  dedup_check: true                     # 중복 TC 파일 생성 방지
  rule_based:
    enabled: true
    include: [positive, missing_required, wrong_type, boundary, invalid_enum]
  ai_augment:
    enabled: true
    max_extra_tc: 3                     # 엔드포인트당 AI 추가 TC 최대 수

runner:
  html_report_path: "./reports/summary.html"
  timeout_seconds: 30

email:
  enabled: false                        # true로 변경 시 활성화
  sender: "your@gmail.com"
  password_env: "SMTP_PASSWORD"
  recipients: ["recipient@example.com"]
```

---

## 6. 사용 방법

### 환경 설정

```bash
pip install -r requirements.txt
export GEMINI_API_KEY=your_api_key_here   # (또는 Windows: set GEMINI_API_KEY=...)
```

### 실행 예시

```bash
# 전체 파이프라인 1회 실행 (파싱 → TC생성 → 테스트 → 이메일)
python main.py run input/QFEapi.yaml

# 파일 감시 모드 (input/ 폴더에 파일을 드롭하면 자동 실행)
python main.py watch

# 파싱 결과만 확인
python main.py parse input/sample_api.yaml

# TC 파일만 생성 (테스트 실행 없음)
python main.py generate input/math_api.yaml

# 설정 파일 경로 지정
python main.py run input/QFEapi.yaml --config config/config.yaml
```

### 이메일 알림 활성화

```bash
# 환경변수로 SMTP 비밀번호 설정 (Gmail 앱 비밀번호 사용 권장)
export SMTP_PASSWORD=your_gmail_app_password

# config.yaml에서 이메일 설정 변경
# email.enabled: true
# email.sender: "your@gmail.com"
# email.recipients: ["team@example.com"]
```

---

## 7. 디렉토리 구조

```
Test_Auto/
├── main.py                    # CLI 진입점 (watch / run / parse / generate)
├── app.py                     # 테스트 대상 FastAPI 샘플 서버
├── config/
│   └── config.yaml            # 전역 설정
├── input/                     # API 명세 파일 드롭 폴더 (watch 모드)
│   ├── QFEapi.yaml
│   ├── math_api.yaml
│   └── sample_api.yaml
├── parsers/
│   ├── openapi_parser.py      # OpenAPI/Swagger YAML·JSON·URL 파서
│   └── python_parser.py       # Python 모듈 함수 시그니처 파서
├── agents/
│   ├── tc_generator.py        # TC 생성 오케스트레이터
│   ├── rule_based_generator.py # Layer 1: 규칙 기반 결정론적 생성
│   └── llm_client.py          # Layer 2: LLM 팩토리 (Gemini/Anthropic/OpenAI)
├── runner/
│   └── test_runner.py         # pytest 실행 + 리포트 수집
├── notifier/
│   └── email_sender.py        # Gmail SMTP 이메일 발송
├── tests/
│   ├── conftest.py            # 공용 pytest 픽스처
│   └── generated/             # 자동 생성된 TC 파일 저장 위치
├── reports/                   # 테스트 결과 리포트
│   ├── summary.html
│   ├── pytest_report.json
│   └── allure-results/
└── requirements.txt
```

---

## 8. 의존성

| 패키지 | 용도 |
|--------|------|
| `pyyaml` | OpenAPI YAML 파싱 |
| `jsonschema` | JSON Schema 검증 |
| `google-generativeai` | Gemini API (기본 AI 제공자) |
| `requests` | HTTP 클라이언트 |
| `fastapi` / `pydantic` | 샘플 서버 (`app.py`) |
| `pytest` | 테스트 프레임워크 |
| `pytest-html` | HTML 리포트 생성 |
| `pytest-json-report` | JSON 리포트 생성 |
| `allure-pytest` | Allure 리포트 연동 |
| `watchdog` | 파일 시스템 감시 (watch 모드) |
| `jinja2` | 리포트 템플릿 렌더링 |

> Anthropic 또는 OpenAI를 사용하려면 `requirements.txt`의 주석 처리된 해당 패키지를 활성화하세요.