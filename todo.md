# QFE API Server 테스트 자동화 프로젝트

> **마감일:** 2026년 4월 22일

---

## 최종 목표

QFE API 서버의 문제점을 파악하고, 개발한 프로그램을 통해 **문제점 해결** 및 **테스트 자동화** 구현

---

## 할 일 목록

### 1. Rule 기반 Test Case Excel 작성

Rule 기반으로 Test Case를 생성하는 항목들을 Excel로 정리

| 항목 | 내용 |
|------|------|
| Rule의 목적 | 각 rule이 존재하는 이유 및 검증 대상 명시 |
| 생성된 Test Case | rule로부터 도출된 test case 목록 |
| 실패 Case | 대표 실패 case 1개 포함 |

---

### 2. AI Agent 연동 및 효과 분석

#### 2-1. AI 모델 교체

- **현재:** Ollama
- **변경:** Claude, Gemini

#### 2-2. 단계별 실험

| 단계 | 설명 | 목적 |
|------|------|------|
| Step 1 | AI만 사용하여 Test Case 생성 | AI 단독 생성 능력 측정 |
| Step 2 | AI에게 rule 수정 권한 부여 후 Test Case 생성 (중복 허용) | 중복 test case 발생 비율 파악 |
| Step 3 | Rule 기반 Test Case 제외 후 AI가 나머지 생성 (중복 불가) | AI 보완 생성 능력 측정 |

---

### 3. 테스트 P/F 조건 세분화

현재 단순 Pass/Fail로 처리되는 조건을 실패 원인별로 분화

**예시:**

- DB에 해당 데이터가 **없어서** Fail
- DB에 데이터가 **있는데도** 데이터를 못 가져와서 Fail

> 실패 원인을 명확히 구분하여 디버깅 효율 향상

---

## 진행 현황

| 작업 | 상태 |
|------|------|
| Rule 기반 Test Case Excel 작성 | ⬜ 미완료 |
| AI Agent 모델 교체 (Claude, Gemini) | ⬜ 미완료 |
| 단계별 AI 효과 분석 (Step 1~3) | ⬜ 미완료 |
| P/F 조건 세분화 | ⬜ 미완료 |