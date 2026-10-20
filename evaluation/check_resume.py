"""Check whether a resume PDF has a usable text layer without printing any resume value.

Usage:
    PYTHONPATH=src python evaluation/check_resume.py /path/outside/repo/resume.pdf
"""
from __future__ import annotations

import sys

from job_agent_core.korean_scope import enforce_korean_mvp_profile, korean_mvp_values
from job_agent_core.resume_parser import ResumeParseError, extract_resume_profile, inspect_resume_file


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("사용법: python evaluation/check_resume.py <이력서.pdf>", file=sys.stderr)
        return 2
    try:
        inspected = inspect_resume_file(argv[1])
    except ResumeParseError as error:
        print(f"[실패] {error}")
        return 1
    text = str(inspected["text"])
    parsed = extract_resume_profile(text)
    profile, scope_warnings = enforce_korean_mvp_profile(parsed["resume_profile"])
    values = korean_mvp_values(profile)

    print(f"페이지 수: {inspected['pages']}")
    print(f"추출 텍스트 길이: {len(text.strip())}자 / 비어 있지 않은 줄: {sum(1 for line in text.splitlines() if line.strip())}")
    print(f"경고 수: {len(parsed['warnings']) + len(scope_warnings)}")
    print("\n이력서 경로별 값 존재 여부 (값 자체는 출력하지 않음):")
    for path, value in values.items():
        print(f"  {'O' if value else '-'}  {path}")
    found = sum(bool(value) for value in values.values())
    print(f"\n값이 있는 경로: {found}/{len(values)}")
    if len(text.strip()) < 200:
        print("[주의] 텍스트가 매우 적습니다. 스캔 PDF일 가능성이 있어 OCR fallback 검토가 필요합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
