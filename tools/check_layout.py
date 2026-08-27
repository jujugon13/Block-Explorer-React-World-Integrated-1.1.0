#!/usr/bin/env python3
"""폴더 정책(AGENTS.md) 위반을 검사한다. CI 게이트용.

사용: python3 tools/check_layout.py src
"""
import json, sys, re
from pathlib import Path

LAYER_WORDS = {"controller", "service", "repository", "dao", "handler_layer", "impl", "util"}

def main(root, manifest=None):
    root = Path(root)
    errors, warnings = [], []
    if manifest is None:
        here = Path(__file__).resolve().parent.parent
        manifest = here / "specs" / "manifest.json"
    manifest = Path(manifest)
    if not manifest.exists():
        print(f"manifest 없음: {manifest}"); return 1

    if not root.exists():
        print(f"경로 없음: {root}"); return 1

    features = json.loads(manifest.read_text(encoding="utf-8")).get("features", [])
    declared = {f["folder"].rstrip("/").split("/")[-1]: f for f in features}

    top = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    top_names = {d.name for d in top}

    # 1. 계층 기준 최상위 폴더 금지
    for name in top_names:
        if name.lower() in LAYER_WORDS:
            errors.append(f"[계층폴더] src/{name} — 기능 기준으로 나눌 것 (AGENTS.md 폴더 정책)")

    # 2. manifest 에 선언된 기능 폴더가 실재하는가
    for name, f in declared.items():
        if name not in top_names:
            warnings.append(f"[미구현] {f['folder']} 없음 — {f['name']}")

    # 3. 선언되지 않은 최상위 폴더
    for name in top_names:
        if name not in declared and name not in {"shared", "platform"}:
            errors.append(f"[미선언] src/{name} — manifest.json features 에 추가하거나 폴더를 옮길 것")

    # 4. 각 기능 폴더에 README 가 있는가
    for d in top:
        if d.name in {"shared"}: continue
        if not (d / "README.md").exists():
            errors.append(f"[README없음] {d}/README.md — 담당 명세 절과 AC ID 표를 둘 것")

    # 5. 저장소 어댑터 분리
    storage = root / "storage"
    if storage.exists():
        subs = {p.name for p in storage.iterdir() if p.is_dir()}
        for need in ("local", "minio", "s3"):
            if need not in subs:
                warnings.append(f"[어댑터] storage/{need} 없음 — 어댑터는 하위 폴더로 분리")

    # 6. 검색 단계 핸들러 분리
    steps = root / "search" / "steps"
    if (root / "search").exists() and not steps.exists():
        errors.append("[단계분리] search/steps 없음 — 단계 핸들러를 파일 1개씩 분리할 것")

    # 7. 폴더 간 직접 참조 (저장소 어댑터를 다른 기능이 직접 import)
    pat = re.compile(r"(?:import|from|require|using).{0,80}storage[./](?:local|minio|s3)", re.I)
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix not in {".java",".py",".ts",".tsx",".kt",".go",".js"}: continue
        if "storage" in f.parts: continue
        try: src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception: continue
        if pat.search(src):
            errors.append(f"[직접참조] {f} — 어댑터를 직접 참조. shared 의 저장소 인터페이스를 쓸 것")

    for w in warnings: print("경고:", w)
    for e in errors: print("위반:", e)
    print(f"\n위반 {len(errors)}건 / 경고 {len(warnings)}건")
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "src"))
