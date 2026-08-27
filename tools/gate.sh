#!/usr/bin/env bash
# 비파괴 회귀 게이트 — 외부 RDS/S3 장애시험을 실행하지 않는다.
# 사용: bash tools/gate.sh [src경로] [단계, 기본 1]
set -u
SRC="${1:-src}"
STAGE="${2:-1}"
FAIL=0
line() { printf '\n%s\n' "──────────────────────────────────────────"; }

line; echo "1) 폴더 정책"
python3 tools/check_layout.py "$SRC" || FAIL=1

line; echo "2) AC 커버리지"
python3 tools/check_coverage.py "$SRC" --stage "$STAGE" || FAIL=1

line; echo "3) 테스트 실행"
if   [ -f gradlew ];      then ./gradlew test           || FAIL=1
elif [ -f package.json ]; then npm test                 || FAIL=1
elif [ -f pyproject.toml ] || [ -f pytest.ini ]; then pytest -q || FAIL=1
elif find "$SRC" -type f -name 'test_*.py' -print -quit | grep -q .; then
  PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$SRC" -t . -p 'test_*.py' || FAIL=1
else echo "  테스트 러너를 찾지 못했다 — 수동 확인 필요"; FAIL=1
fi

line
if [ "$FAIL" -eq 0 ]; then echo "비파괴 회귀 게이트 통과"; else echo "비파괴 회귀 게이트 미통과"; fi
exit "$FAIL"
