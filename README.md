# Apply Local — 한국 채용 지원서 자동 입력 Agent

Apply Local은 PDF 이력서를 읽어 한국 채용 사이트의 지원서 양식에 개인정보와 학력을 자동으로 입력하는 **로컬 우선(local-first) 데스크톱 Agent**입니다.

로그인, CAPTCHA, 본인 인증, 약관 동의, 최종 제출처럼 사람이 결정해야 하는 단계는 사용자에게 맡기고, 반복적인 입력 작업만 자동화합니다. 이력서와 개인정보는 사용자 컴퓨터 밖으로 전송하지 않습니다.

## 주요 기능

- **이력서 분석**: 텍스트형 PDF에서 이름, 생년월일, 연락처, 이메일, 주소, 보유 기술, 대학교·대학원 학력을 추출합니다.
- **지원서 필드 검사**: 실제 브라우저 DOM에서 입력 필드, 라벨, 영역, 선택지, 컨트롤 종류를 찾습니다.
- **의미 매핑**: 웹페이지의 필드가 이력서의 어떤 항목을 요구하는지 판단합니다.
  - `성` + `이름`처럼 나뉜 이름 필드와 단일 `성명` 필드를 구분합니다.
  - `010 / 1234 / 5678` 형태의 세 칸 휴대폰 입력을 인식합니다.
  - 비상연락처·보호자·추천인처럼 **지원자 본인이 아닌 사람**의 필드에는 입력하지 않습니다.
- **형식 변환**: 사이트의 placeholder, pattern, maxlength에 맞춰 전화번호·날짜·평점 형식을 맞춥니다.
- **다양한 입력 컨트롤**: text, select, radio, 자동완성, 사용자 정의 선택, 연/월/일 select, 달력 위젯.
- **입력 후 검증**: 입력한 값을 브라우저에서 다시 읽어 확인합니다. 실패하면 대체 형식으로 한 번만 재시도하고, 그래도 실패하면 값을 지운 뒤 사용자에게 알립니다.
- **다단계 지원서**: 최종 제출과 명확히 구분되는 `다음` 버튼만 안전하게 누릅니다.

## 안전 원칙

- 아이디·비밀번호를 입력하지 않고, CAPTCHA·MFA를 처리하거나 우회하지 않습니다.
- **최종 제출 버튼은 절대 자동으로 누르지 않습니다.**
- 페이지에 이미 입력된 값은 덮어쓰지 않습니다.
- 약관 동의 체크박스, 자기소개서, 민감한 신원 정보는 자동 입력하지 않습니다.
- HTTPS 페이지만 열고, 다른 도메인으로 이동할 때는 사용자 승인을 받습니다.
- 웹페이지의 문구를 Agent에 대한 지시로 해석하지 않습니다.
- 이력서 원문과 개인정보는 프로세스 메모리에만 두고, 작업을 취소하거나 프로그램을 종료하면 지웁니다. SQLite에는 작업 상태 메타데이터만 저장합니다.

## 동작 흐름

```text
PDF 이력서 ─▶ 이력서 파서 ─▶ ResumeProfile (메모리)
                                   │
채용 페이지 ─▶ Form Inspector ─▶ 필드 목록
                                   ▼
                     Mapper (규칙 + 구조 판단)
                                   ▼
                     Planner ─▶ Formatter / Widget Adapter
                                   ▼
                     Executor (Playwright) ─▶ Readback Validator
                                   │
                ┌──────────────────┼──────────────────┐
              통과              재시도 1회         실패 → 값 삭제 + 사용자 확인
```

화면은 PySide6 네이티브 창이고, 내부적으로 임의 포트·임의 토큰을 쓰는 로컬 FastAPI 프로세스를 통해 핵심 모듈을 호출합니다.

## 설치

Python 3.11 이상이 필요합니다.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
```

## 실행

```bash
.venv/bin/python run_local.py
```

1. 데스크톱 창에서 PDF 이력서를 `선택`하고 채용 지원 페이지 URL을 입력한 뒤 `이력서 읽고 준비`를 누릅니다.
2. `브라우저 열기`를 누르고, 열린 브라우저에서 로그인·본인 인증을 직접 완료해 지원서 화면으로 이동합니다.
3. `현재 페이지 확인`으로 검출 결과를 보고, `확인된 항목만 입력`으로 자동 입력합니다.
4. 다음 단계가 있으면 `다음 페이지`를 누르거나 직접 이동한 뒤 다시 확인합니다.
5. 입력 결과를 직접 검토한 뒤 **최종 제출은 사용자가 직접** 합니다.

## 테스트

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

테스트는 LLM이나 실제 채용 사이트에 접속하지 않습니다. 브라우저 통합 테스트는 로컬 Chromium과 합성 HTML 양식으로 입력·재확인·재시도·자동 제출 금지를 검사합니다. 테스트용 이력서 `resume_kr_table_synthetic.pdf`는 가상의 데이터입니다.

## Before / After 평가

`evaluation/` 폴더에는 규칙 기반 Before 버전과 개선된 After 버전을 같은 조건에서 비교하는 도구가 있습니다. 결과 파일에는 이력서 값이나 URL을 저장하지 않으며, 사이트는 익명 별칭으로만 기록합니다.

| 스크립트 | 역할 |
|---|---|
| `check_resume.py` | PDF에 추출 가능한 텍스트가 있는지, 항목별 값 존재 여부만 확인 |
| `record_run.py` | 실제 사이트에서 검사·입력 결과와 소요 시간을 기록 |
| `score_runs.py` | 정답 라벨(`evaluation/labels/*.csv`)과 비교해 지표 계산 |

주요 지표: 필드 매핑 정확도, 자동 입력 성공률, 검증 통과율, 잘못된 입력(Unsafe fill), 사용자 개입률, 재시도 횟수, 지연 시간.

```bash
.venv/bin/python evaluation/check_resume.py /path/to/resume.pdf
.venv/bin/python evaluation/record_run.py --variant after --site site-a --resume /path/to/resume.pdf --url "https://..."
.venv/bin/python evaluation/score_runs.py
```

## 프로젝트 구조

```text
run_local.py                 로컬 API + 데스크톱 화면 실행
src/job_agent_core/
  desktop.py                 PySide6 화면
  api.py                     로컬 FastAPI 작업 API
  resume_parser.py           PDF 검사와 이력서 항목 추출
  models.py, names.py        이력서·이름 모델, 이름 분리 규칙
  form_inspector.py          브라우저 DOM 필드 검사
  korean_scope.py            필드 → 이력서 항목 매핑 (이름 구조, 휴대폰 칸, 제3자 보호)
  mapping.py                 공통 필드/매핑 모델과 MappingProvider 인터페이스
  planning.py, worker.py     입력 계획
  widgets.py                 컨트롤별 입력 방식과 형식 변환
  browser_session.py         격리된 Playwright 브라우저와 도메인 정책
  browser_worker.py          검사·입력·재확인·재시도·다음 단계
  security.py, validation.py URL/제출 버튼 안전 규칙, 검증
  tasks.py                   작업 상태(SQLite)와 메모리 내 민감 데이터 보관
evaluation/                  Before/After 평가 도구
tests/                       단위·통합 테스트
```

## 현재 한계와 다음 단계

- 스캔(이미지형) PDF는 아직 지원하지 않습니다. OCR이 필요합니다.
- 경력, 자기소개서, 첨부파일 업로드는 자동 입력 범위에 포함하지 않습니다.
- 복잡한 iframe, Shadow DOM, 사이트 전용 위젯은 별도 어댑터가 필요할 수 있습니다.
- 일부 사이트는 제출 전에도 임시 저장을 할 수 있습니다.
- 예정: 명시적인 Agent 상태 그래프, 로컬 Ollama + 익명 필드 사례 RAG로 모호한 필드 매핑, 여러 URL 순차 처리.
