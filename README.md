<p align="center"><img src="docs/logo.png" width="128" alt="Apply Local 아이콘"></p>

<h1 align="center">Apply Local</h1>

<p align="center">한국 채용 지원서 자동 입력 Agent</p>

Apply Local은 PDF 이력서를 읽어 한국 채용 사이트의 지원서 양식에 개인정보와 학력을 자동으로 입력하는 **로컬 우선(local-first) 데스크톱 앱**입니다.

로그인, 본인 인증, 약관 동의, 최종 제출처럼 사람이 결정해야 하는 단계는 사용자에게 맡기고, 반복적인 입력만 대신합니다. 이력서와 개인정보는 내 컴퓨터 밖으로 나가지 않으며, AI도 이 컴퓨터에서 실행되는 모델만 사용합니다.

## 주요 기능

- **이력서 읽기**: 이름(한글·영문), 생년월일, 연락처, 이메일, 주소, 병역·장애·보훈, 고등학교·대학교·대학원 학력(편입 포함)을 추출합니다. 읽은 값은 미리보기에서 확인하고 고칠 수 있습니다.
- **지원서 이해**: 사이트마다 다른 입력 칸을 라벨과 구조로 읽고, 각 칸이 이력서의 어떤 항목인지 판단합니다. 로컬 AI가 처음 보는 양식도 함께 이해합니다.
- **자동 입력과 확인**: 전화번호·날짜·학점을 사이트 형식에 맞춰 넣고, 넣은 값을 다시 읽어 확인합니다. 학력 「추가」 블록 열기와 우편번호 검색 창도 처리합니다.
- **모든 칸에 결과 표시**: 입력 완료, 기존 값 유지, 확인 필요, 이력서에 없음, 직접 처리 중 하나로 모든 칸의 상태를 보여 줍니다. 「확인 필요」 항목은 화면에서 바로 고를 수 있습니다.
- **작은 화면**: 화면 위쪽의 캡슐 바와 메뉴 막대의 네잎클로버 아이콘으로 진행 상황을 알려 줍니다.

## 다운로드 · 설치

**[최신 버전 다운로드 (Releases)](https://github.com/wwkddus/apply_agent/releases/latest)**

| 컴퓨터 | 받을 파일 |
|---|---|
| Mac (Apple Silicon) | `ApplyLocal-mac-AppleSilicon.zip` |
| Windows 10/11 | `ApplyLocal-windows.zip` |

Mac에는 Chrome 브라우저가 필요합니다. Windows는 기본으로 설치된 Edge를 씁니다. 그 밖에 따로 설치할 것은 없습니다.

### Mac
1. zip을 풀고 `Apply Local.app`을 「응용 프로그램」 폴더로 옮깁니다.
2. 처음 실행할 때 「확인되지 않은 개발자」 경고가 뜨면 「시스템 설정 → 개인정보 보호 및 보안」에서 **「그래도 열기」**를 누릅니다.
3. 「손상되었기 때문에 열 수 없습니다」라고 나오면 터미널에서 한 번 실행합니다.
   ```bash
   xattr -dr com.apple.quarantine "/Applications/Apply Local.app"
   ```

### Windows
1. zip을 풀고 `Apply Local` 폴더 안의 `Apply Local.exe`를 실행합니다. 폴더 안의 다른 파일은 지우지 마세요.
2. 「Windows의 PC 보호」 창이 뜨면 **「추가 정보」 → 「실행」**을 누릅니다.

## 사용 방법

1. **이력서 올리기**: PDF를 끌어 놓고, 읽은 값을 확인합니다. 이력서가 없다면 **「템플릿 열기」**로 브라우저에서 템플릿을 고친 뒤 PDF로 저장해 올리면 됩니다.
2. **채용 페이지 열기**: 지원서 URL을 넣고 `브라우저 열기`를 누른 뒤, 열린 브라우저에서 로그인·본인 인증을 직접 마칩니다.
3. **자동 입력**: 지원서 화면에서 `자동 입력 시작`을 누르면 칸마다 진행 상황이 보입니다.
4. **확인과 제출**: 「확인 필요」 항목을 고르거나 고친 뒤 `적용`합니다. **저장과 최종 제출은 직접** 합니다.

## AI 보조 기능

처음 실행하면 AI 보조 기능을 쓸지 고르는 화면이 나옵니다. AI는 이 컴퓨터에서 실행되는 [Ollama](https://ollama.com)를 사용하며, 처음 보는 지원서 칸을 이해하고 규칙으로 못 채운 항목을 이력서 원문과 한 번 더 대조합니다.

- **설치하고 사용 (권장)**: Ollama 공식 설치 파일을 받아 서명을 확인한 뒤 설치하고, AI 모델(약 9.9GB)을 받습니다. macOS 14 이상, 메모리 16GB 이상을 권장합니다.
- **AI 없이 사용**: 규칙만으로 입력합니다.
- 나중에 바꾸려면 메뉴의 **「AI 기능 설정…」**을 누릅니다.

## 개인정보와 안전

- 이력서와 개인정보는 내 컴퓨터에만 있고, 앱을 끄면 메모리에서 지웁니다.
- 아이디·비밀번호 입력, CAPTCHA, 본인 인증은 하지 않습니다.
- **최종 제출·저장 버튼은 절대 자동으로 누르지 않습니다.**
- 이미 입력된 값은 덮어쓰지 않습니다.
- 약관 동의, 자기소개서, 첨부파일은 자동으로 처리하지 않습니다.
- 작업용 브라우저는 평소 쓰는 Chrome과 분리된 새 프로필을 쓰고, 작업이 끝나면 지웁니다.

## 개발

Python 3.11 이상이 필요합니다.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
.venv/bin/python run_local.py
```

테스트:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
```

앱 빌드:

```bash
.venv/bin/pip install "pyinstaller>=6.10,<7"
.venv/bin/pyinstaller packaging/ApplyLocal.spec --noconfirm
```

GitHub Actions의 「Release」 워크플로가 Mac·Windows 앱을 빌드합니다. `v<버전>` 태그를 push하면 Release에 올라갑니다.

## 구조

```text
src/job_agent_core/
  desktop.py, tray.py        데스크톱 화면과 메뉴 막대 아이콘 (PySide6)
  api.py, launcher.py        로컬 작업 API와 앱 시작
  resume_parser.py           이력서 읽기 (+ resume_llm.py 로컬 AI 보조)
  form_inspector.py          지원서 입력 칸 찾기
  korean_scope.py, llm_mapping.py  칸 ↔ 이력서 항목 연결 (규칙 + 로컬 AI)
  browser_worker.py          입력·확인·재시도 (Playwright)
  ollama_setup.py            AI 기능 설치
packaging/                   앱 빌드 설정
tests/                       테스트
```
