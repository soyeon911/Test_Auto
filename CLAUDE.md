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

[유의사항]
./qfe-api-server-no-license 폴더 아래는 절대 건들면 안 됨


## Rules for Test Case Generation

1. Test cases must be generated primarily from the QFEAPI/Swagger specification.
2. Each test case must have a clear purpose and traceable generation reason.
3. Generated test cases must be classifiable by type such as positive, negative, wrong_type, missing_required, out_of_range, and invalid_format.
4. Rule-based test cases must be reproducible and deterministic.
5. AI-generated test cases must be explicitly distinguishable from rule-based test cases.


## Purpose of Rule-Based Generation

- Detect schema-level validation issues from the API specification.
- Cover deterministic negative cases derived from field constraints.
- Ensure baseline coverage before any AI-based expansion.

## Duplicate Handling Rules

- In Step 2, duplicate test cases between rule-based and AI-generated sets are allowed.
- In Step 2, duplicates must be counted and reported.
- In Step 3, duplicate test cases must never be included in the final merged set.
- Duplicate detection must consider endpoint, intent, input structure, and expected outcome.

## Experiment Modes

### Step 1
Use AI only to generate test cases.

### Step 2
Use both rule-based and AI-generated test cases.
Allow duplicate cases and report the duplicate count.

### Step 3
Use both rule-based and AI-generated test cases.
Exclude any AI-generated test case that duplicates an existing rule-based test case.

## Pass/Fail Classification Rules

A failed test result must not be classified as a single generic failure.
Failures must be further categorized by cause, including but not limited to:

- validation failure
- missing DB data
- DB lookup failure despite existing data
- internal server processing failure
- unexpected response mismatch



## Reporting Rules

The generated report must include:
- rule name or generation source
- rule purpose
- generated test case description
- test case type
- execution result
- failure classification
- duplicate 여부


## Reporting Rules

The generated report must include:
- rule name or generation source
- rule purpose
- generated test case description
- test case type
- execution result
- failure classification
- duplicate 여부

## Model-Agnostic Evaluation Rules

When switching the AI agent from Ollama to Claude or Gemini:
- the same input specification must be used
- the same comparison criteria must be maintained
- generated test cases must be evaluated under the same duplicate and coverage rules