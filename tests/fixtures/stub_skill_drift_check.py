"""Stand-in for XYZ-forge's utils/py/skill_drift_check.py, honouring its `--json` contract.

sync.py calls the forge checker through a subprocess; this repo has no forge checkout, so the tests
install this stub at <fixture forge>/utils/py/skill_drift_check.py. Same arguments, same JSON keys
(`ok`, `drifted`, `unrecognized`; entries carry `skill`, `vendored_path`, `canonical_path`), same
exit codes (0 clean, 1 drift, 2 usage).
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--canonical", required=True); p.add_argument("--collection", required=True)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    canonical, collection = Path(args.canonical), Path(args.collection)
    skills = canonical / "skills" if (canonical / "skills").is_dir() else canonical
    if not skills.is_dir() or not collection.is_dir():
        return 2
    result, names = {"drifted": [], "ok": [], "unrecognized": []}, set()
    for skill_md in sorted(list(skills.glob("*/SKILL.md")) + list(skills.glob("*/*/SKILL.md"))):
        name = skill_md.parent.name
        names.add(name)
        vendored = collection / name / "SKILL.md"
        if not vendored.is_file():
            continue
        entry = {"skill": name, "canonical_sha256": digest(skill_md), "vendored_sha256": digest(vendored)}
        if entry["canonical_sha256"] != entry["vendored_sha256"]:
            entry.update(canonical_path=str(skill_md), vendored_path=str(vendored))
            result["drifted"].append(entry)
        else:
            result["ok"].append(entry)
    for vendored_md in sorted(collection.glob("*/SKILL.md")):
        if vendored_md.parent.name not in names:
            result["unrecognized"].append({"skill": vendored_md.parent.name, "vendored_path": str(vendored_md)})
    print(json.dumps(result, indent=2))
    return 1 if result["drifted"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
