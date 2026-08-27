#!/usr/bin/env bash
# 파괴적 외부시험: 승인된 test/staging RDS와 S3에만 실행한다.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "16단계 파괴적 외부시험: 16-1~16-6가 모두 PASS여야 통과"
exec "${PYTHON:-python3}" tools/run_stage16_external.py --scenario all
