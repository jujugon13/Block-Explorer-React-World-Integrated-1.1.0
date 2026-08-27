#!/usr/bin/env python3
"""acceptance criteria 커버리지를 검사한다. 완료 판정 게이트용.

사용:
  python3 tools/check_coverage.py src
  python3 tools/check_coverage.py src --feature F04
  python3 tools/check_coverage.py src --stage 3
"""
import json, re, sys
from pathlib import Path

TEST_HINTS = ("test", "spec", "__tests__")
CODE_EXT = {".java", ".py", ".ts", ".tsx", ".kt", ".go", ".js", ".rb", ".cs"}
AC_PAT = re.compile(r"AC[-_]([A-Z]{2,6})[-_](\d{2,3})")
STAGE_FEATURES = {
    1: {"F01"},
    2: {"F01", "F02", "F03"},
    3: {"F01", "F02", "F03", "F04"},
    4: {"F01", "F02", "F03", "F04", "F05", "F06"},
    5: {"F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08"},
    6: {f"F{i:02d}" for i in range(1, 12)},
    # 7단계는 검색 뼈대만 만들며 AC-RS-*는 8단계에서 활성화한다.
    7: {f"F{i:02d}" for i in range(1, 12)},
    8: {f"F{i:02d}" for i in range(1, 14)},
    9: {f"F{i:02d}" for i in range(1, 17)},
    10: {f"F{i:02d}" for i in range(1, 18)},
    11: {f"F{i:02d}" for i in range(1, 18)},
    12: {f"F{i:02d}" for i in range(1, 18)},
    13: {f"F{i:02d}" for i in range(1, 18)},
    14: {f"F{i:02d}" for i in range(1, 18)},
    15: {f"F{i:02d}" for i in range(1, 18)},
    16: {f"F{i:02d}" for i in range(1, 18)},
    17: {f"F{i:02d}" for i in range(1, 18)},
}


def norm(match):
    return f"AC-{match.group(1)}-{match.group(2)}"


def collect_declared(manifest, only=None):
    data = json.loads(manifest.read_text(encoding="utf-8"))
    out = {}
    features = {feature["id"]: feature for feature in data.get("features", [])}
    for feature in data.get("features", []):
        if only and feature["id"] not in only:
            continue
        for acceptance_id in feature.get("acceptance_ids", []):
            out.setdefault(acceptance_id, feature)
    extension_path = manifest.parent.parent / "IMPLEMENTATION_MANIFEST.json"
    if extension_path.exists():
        extensions = json.loads(extension_path.read_text(encoding="utf-8"))
        for extension in extensions.get("feature_extensions", []):
            feature = features.get(extension["id"])
            if feature is None:
                raise ValueError(f"알 수 없는 feature 확장: {extension['id']}")
            if only and feature["id"] not in only:
                continue
            for acceptance_id in extension.get("acceptance_ids", []):
                out.setdefault(acceptance_id, feature)
    return out


def collect_implemented(root):
    found = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in CODE_EXT:
            continue
        if not any(hint in str(path).lower() for hint in TEST_HINTS):
            continue
        blob = str(path) + "\n"
        try:
            blob += path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for match in AC_PAT.finditer(blob):
            found.setdefault(norm(match), set()).add(str(path))
    return found


def main(argv):
    root = Path(argv[0]) if argv else Path("src")
    only = None
    if "--feature" in argv:
        only = set(argv[argv.index("--feature") + 1].split(","))
    if "--stage" in argv:
        if only:
            print("--feature 와 --stage 는 함께 사용할 수 없습니다.")
            return 2
        try:
            only = STAGE_FEATURES[int(argv[argv.index("--stage") + 1])]
        except (IndexError, KeyError, ValueError):
            print("--stage 는 1~17 중 하나여야 합니다.")
            return 2

    manifest = Path(__file__).resolve().parent.parent / "specs" / "manifest.json"
    if not manifest.exists():
        print(f"manifest 없음: {manifest}")
        return 1
    if not root.exists():
        print(f"경로 없음: {root}")
        return 1

    declared = collect_declared(manifest, only)
    implemented = collect_implemented(root)
    missing = sorted(set(declared) - set(implemented))
    unknown = sorted(set(implemented) - set(declared))
    covered = sorted(set(declared) & set(implemented))
    total = len(declared)
    percent = (len(covered) / total * 100) if total else 0.0
    print(f"=== AC 커버리지 {len(covered)}/{total} ({percent:.1f}%) ===\n")

    by_feature = {}
    for acceptance_id in missing:
        feature = declared[acceptance_id]
        by_feature.setdefault(feature["id"] + " " + feature["name"], []).append(acceptance_id)
    if by_feature:
        print("[미구현 — 테스트가 없는 AC]")
        for name in sorted(by_feature):
            print(f"  {name}: {', '.join(by_feature[name])}")
        print()
    if unknown:
        print("[미선언 — 테스트에는 있으나 manifest 에 없는 AC]")
        print("  " + ", ".join(unknown))
        print("  → 오타이거나 manifest 갱신이 필요하다\n")

    duplicates = {
        acceptance_id: paths
        for acceptance_id, paths in implemented.items()
        if len(paths) > 1 and acceptance_id in declared
    }
    if duplicates:
        print("[중복 — 여러 파일에 같은 AC]")
        for acceptance_id, paths in sorted(duplicates.items()):
            print(f"  {acceptance_id}: {len(paths)}곳")
        print()

    ok = not missing and not unknown
    print("판정:", "통과" if ok else "미통과")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
