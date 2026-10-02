#!/usr/bin/env python3
"""Local skill collection, verified archives and shared transaction persistence (Python 3.9+)."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile

sys.dont_write_bytecode = True
SCHEMA = 1
STATE = ".deploy-skills.json"
PENDING = ".deploy-skills-pending.json"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
RESERVED = {"backups", "catalog", "changelog", "targets", "intake", "sync"}
# A repository-root bundle (a folder with MANIFEST.txt, e.g. this manager's own repo) leaves its
# repo-only files behind. `.git` is never copied from any source.
PROJECTED_ROOT_IGNORES = frozenset({
    ".git", ".gitignore", ".xyz-forge-revision", "MANIFEST.txt", "LICENSE", "LICENSE-COMMERCIAL.md",
    "tests", ".github",
})
SOURCE_RULES = ("any", "git", "clean")


class DeployError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise DeployError(message)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_name(value):
    require(isinstance(value, str) and len(value) <= 64 and NAME.fullmatch(value)
            and value not in RESERVED, f"Invalid/reserved skill name: {value!r}")
    return value


def within(child, parent):
    return child != parent and parent in child.parents


def location(raw):
    """Validate a concrete path, including every existing symlink ancestor."""
    require(str(raw).strip() and not str(raw).startswith(("http:", "https:")), "Local path required")
    path = Path(os.path.abspath(Path(raw).expanduser()))
    for part in [path, *path.parents]:
        require(not part.is_symlink(), f"Symlinked root/ancestor refused: {part}; select its real path")
    require(path != Path(path.anchor) and path != Path.home(), f"Protected root: {path}")
    return path


def regular_bytes(path):
    require(path.is_file() and not path.is_symlink(), f"Expected regular file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), f"Not a regular file: {path}")
        return stream.read()


def read_json(path):
    try:
        return json.loads(regular_bytes(path))
    except (ValueError, UnicodeError) as exc:
        raise DeployError(f"Corrupt JSON {path}: {exc}") from exc


def atomic_bytes(path, data):
    location(path.parent)
    require(not path.is_symlink(), f"Refusing control-file symlink: {path}")
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def skill_info(folder, require_folder_name=True):
    """Read the two discovery fields; leave the rest of the author's YAML untouched."""
    text = regular_bytes(folder / "SKILL.md").decode("utf-8")
    require(text.startswith("---\n") and "\n---" in text[4:], f"Missing frontmatter: {folder}/SKILL.md")
    header = text[4:].split("\n---", 1)[0]
    fields = {}
    lines = header.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^(name|description):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            require(key not in fields, f"Duplicate {key} in {folder}/SKILL.md")
            if value in ("|", "|-", "|+", ">", ">-", ">+"):
                block = []
                for following in lines[i + 1:]:
                    if following and not following[0].isspace():
                        break
                    block.append(following.strip())
                value = " ".join(block).strip()
            elif value.startswith('"'):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise DeployError(f"Invalid quoted {key} in {folder}") from exc
            elif value.startswith("'"):
                require(value.endswith("'"), f"Unclosed {key} in {folder}")
                value = value[1:-1].replace("''", "'")
            fields[key] = value
    name = safe_name(fields.get("name"))
    if require_folder_name:
        require(folder.name == name, f"Folder/name mismatch: {folder.name} != {name}")
    require(isinstance(fields.get("description"), str) and fields["description"].strip(),
            f"Missing description: {folder}/SKILL.md")
    return fields


def package_info(folder):
    """Read any skill folder (skills-army-mini#3). The frontmatter name is authoritative and becomes the collection
    folder name; the source folder may be named anything. `.git` is never copied. A folder carrying
    MANIFEST.txt is a repository-root bundle and also leaves its repo-only files behind."""
    info = skill_info(folder, require_folder_name=False)
    if (folder / "MANIFEST.txt").is_file():
        manifest = set(regular_bytes(folder / "MANIFEST.txt").decode("utf-8").splitlines())
        require("SKILL.md" in manifest, f"MANIFEST.txt does not list SKILL.md: {folder}")
        return info, PROJECTED_ROOT_IGNORES
    return info, frozenset({".git"})


def snapshot(folder, ignored=()):
    """Hash bytes, modes and internal relative link text without traversing links."""
    require(folder.is_dir() and not folder.is_symlink(), f"Expected real skill folder: {folder}")
    entries = {"": {"kind": "dir", "mode": stat.S_IMODE(folder.stat().st_mode)}}
    directory_edges = {}
    for base, dirs, files in os.walk(folder, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        if Path(base) == folder:
            dirs[:] = [name for name in dirs if name not in ignored]
            files = [name for name in files if name not in ignored]
        if ".git" in ignored:  # sources (skills-army-mini#3): never take .git at any depth
            dirs[:] = [name for name in dirs if name != ".git"]
            files = [name for name in files if name != ".git"]
        directory_edges[Path(base).resolve()] = []
        for name in sorted(dirs + files):
            path = Path(base) / name
            rel = path.relative_to(folder).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                require(not os.path.isabs(target), f"Absolute payload link: {path}")
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise DeployError(f"Dangling/cyclic payload link: {path}") from exc
                require(within(resolved, folder.resolve()) and resolved not in path.parents,
                        f"External/cyclic payload link: {path}")
                entries[rel] = {"kind": "link", "text": target, "mode": stat.S_IMODE(mode)}
                if resolved.is_dir():
                    directory_edges[Path(base).resolve()].append(resolved)
            elif stat.S_ISDIR(mode):
                entries[rel] = {"kind": "dir", "mode": stat.S_IMODE(mode)}
                directory_edges[Path(base).resolve()].append(path.resolve())
            elif stat.S_ISREG(mode):
                entries[rel] = {"kind": "file", "mode": stat.S_IMODE(mode),
                                "sha256": hashlib.sha256(regular_bytes(path)).hexdigest()}
            else:
                raise DeployError(f"Special file refused: {path}")
    visited, active = set(), set()
    def visit(directory):
        require(directory not in active, f"Cyclic directory links: {directory}")
        if directory in visited:
            return
        active.add(directory)
        for child in directory_edges.get(directory, []):
            visit(child)
        active.remove(directory)
        visited.add(directory)
    visit(folder.resolve())
    require(len(entries) > 1, f"Empty skill: {folder}")
    return entries


def digest(folder, ignored=()):
    return hashlib.sha256(json.dumps(snapshot(folder, ignored), sort_keys=True).encode()).hexdigest()


def source_record(raw, rules=None):
    """Receipt for a skill source (skills-army-mini#3). Any local folder qualifies: the receipt always records the
    path, digest and time, and adds repository/commit/branch/dirty when the folder is in a git repo.
    A device may opt into source rules (`intake.py settings`); none apply by default."""
    source = Path(raw).expanduser().resolve(strict=True)
    require(source.is_dir(), f"Source is not a folder: {source}")
    info, ignored = package_info(source)
    record = {**info, "source": str(source), "digest": digest(source, ignored), "updated": utc(),
              "prerequisites": []}
    def git(*args):
        try:
            run = subprocess.run(["git", "--no-optional-locks", "-C", str(source), *args],
                                 text=True, capture_output=True)
        except OSError:
            return None
        return run.stdout.strip() if run.returncode == 0 else None
    repo = git("rev-parse", "--show-toplevel")
    if repo:
        record.update(repository=str(Path(repo).resolve()), commit=git("rev-parse", "HEAD"),
                      branch=git("rev-parse", "--abbrev-ref", "HEAD"),
                      dirty=bool(git("status", "--porcelain", "--", str(source))))
    check_source_rules(record, rules or {})
    return source, record, ignored


def check_source_rules(record, rules):
    """Opt-in, per-device source rules. Skills Army sets no source policy unless the operator does."""
    mode = rules.get("mode", "any")
    if mode in ("git", "clean"):
        require(record.get("repository") and record.get("commit"),
                f"Source rule '{mode}' (intake.py settings): {record['source']} is not in a committed git repository")
    if mode == "clean":
        require(not record.get("dirty"), f"Source rule 'clean' (intake.py settings): {record['source']} has uncommitted changes")
    allowed = rules.get("repositories") or []
    if allowed:
        require(record.get("repository") in allowed,
                f"Source rule (intake.py settings): {record['source']} is outside the allowed repositories {allowed}")


def defaults():
    return {"schema": SCHEMA, "targets": [
        {"id": "claude", "path": "~/.claude/skills", "enabled": False,
         "consumers": ["VS Code Claude Code extension"]},
        {"id": "codex", "path": "~/.agents/skills", "enabled": False,
         "consumers": ["VS Code Codex extension", "Codex desktop app"]},
        {"id": "antigravity", "path": "~/.gemini/config/skills", "enabled": False,
         "consumers": ["Antigravity app"]},
        {"id": "zcode", "path": "~/.zcode/skills", "enabled": False,
         "consumers": ["Zcode GLM app"]}]}


def validate_targets(root, config):
    require(isinstance(config, dict) and config.get("schema") == SCHEMA
            and isinstance(config.get("targets"), list), "Invalid targets.json schema")
    ids, paths = set(), []
    for target in config["targets"]:
        require(isinstance(target, dict) and isinstance(target.get("id"), str)
                and NAME.fullmatch(target["id"]) and target["id"] not in ids,
                "Invalid/duplicate target id")
        ids.add(target["id"])
        require(type(target.get("enabled")) is bool and isinstance(target.get("path"), str)
                and isinstance(target.get("consumers"), list)
                and all(isinstance(c, str) for c in target["consumers"]), "Invalid target fields")
        path = location(target["path"])
        require(path != root and not within(path, root) and not within(root, path),
                f"Collection/target overlap: {path}")
        require(not path.exists() or path.is_dir(), f"Target is not a directory: {path}")
        for other in paths:
            require(not within(path, other) and not within(other, path), f"Nested target roots: {path}")
        paths.append(path)
    # Device-wide settings (skills-army-mini#3), written only by `intake.py settings`.
    require(config.get("drift", "warn") in ("warn", "refuse"), "Invalid drift setting (warn|refuse)")
    require(config.get("canonical") is None or isinstance(config["canonical"], str), "Invalid canonical setting")
    rules = config.get("source_rules", {})
    require(isinstance(rules, dict) and set(rules) <= {"mode", "repositories"}
            and rules.get("mode", "any") in SOURCE_RULES
            and isinstance(rules.get("repositories", []), list)
            and all(isinstance(r, str) for r in rules.get("repositories", [])), "Invalid source_rules setting")
    return config


def validate_state(root, state):
    require(isinstance(state, dict) and state.get("schema") == SCHEMA
            and state.get("root") == str(root), "Invalid metadata schema/root; do not prune links")
    require(re.fullmatch(r"[a-f0-9]{32}", state.get("collection", "")) is not None,
            "Invalid collection identity")
    require(all(isinstance(state.get(k), dict) for k in ("skills", "links")), "Invalid metadata records")
    for name, record in state["skills"].items():
        safe_name(name)
        require(isinstance(record, dict) and isinstance(record.get("digest"), str)
                and re.fullmatch(r"[a-f0-9]{64}", record["digest"])
                and isinstance(record.get("prerequisites", []), list)
                and all(isinstance(p, str) for p in record.get("prerequisites", [])), "Invalid skill receipt")
    for key, record in state["links"].items():
        require(isinstance(record, dict), "Invalid owned-link receipt")
        name = safe_name(record.get("name"))
        target = location(record.get("root", ""))
        require(key == str(target / name) and record.get("text") == str(root / name)
                and record.get("collection") == state["collection"], "Invalid owned-link identity")
        require(not within(target, root) and not within(root, target) and target != root,
                "Owned target overlaps collection")
    return state


def load(root, pending=False):
    require(root.is_dir(), f"Collection missing: {root}; run intake.py init")
    require(pending or not (root / PENDING).exists(), "Interrupted operation: run intake.py recover --apply")
    state = validate_state(root, read_json(root / STATE))
    config = validate_targets(root, read_json(root / "targets.json"))
    for name in ("intake.py", "sync.py"):
        link = root / name
        require(link.is_symlink() and os.readlink(link) in
                (f"skills-army-hq/scripts/{name}", f"deploy-skills/scripts/{name}")
                and link.is_file(), f"Manager entry missing or changed: {link}; preserve state and inspect recovery")
    return state, config


def inventory(root, state, allow_missing=()):
    found = {}
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.name == "backups":
            continue
        if path.is_dir() or path.is_symlink() and path.name not in ("intake.py", "sync.py"):
            require(not path.is_symlink(), f"Collection payload must be a real folder: {path}")
            info = skill_info(path)
            require(info["name"].casefold() not in {n.casefold() for n in found}, "Case-fold skill collision")
            found[info["name"]] = {**info, "digest": digest(path)}
    missing = set(state["skills"]) - set(found) - set(allow_missing)
    require(not missing, f"Unexplained missing skills: {sorted(missing)}; explicitly remove to acknowledge")
    return found


@contextlib.contextmanager
def locked(root):
    try:
        import fcntl
    except ImportError as exc:
        raise DeployError("This build requires POSIX advisory locks; Windows support is unverified") from exc
    location(root)
    lock = root / ".deploy-skills.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "Lock must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("Collection busy: another intake/sync operation holds the OS lock") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} started={utc()}\n".encode())
        yield
    finally:
        os.close(fd)


def archive(root, folder):
    before = snapshot(folder)
    destination = root / "backups"
    location(destination)
    destination.mkdir(exist_ok=True)
    stem = f"{folder.name}-{utc()[:10]}"
    index = 1
    while True:
        path = destination / f"{stem}{'' if index == 1 else '-' + str(index).zfill(2)}.zip"
        try:
            stream = path.open("xb")
            break
        except FileExistsError:
            index += 1
    try:
        with stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
            for rel, entry in before.items():
                kind = entry["kind"]
                member = zipfile.ZipInfo(f"{folder.name}/{rel}" + ("/" if kind == "dir" and rel else ""))
                member.create_system = 3
                typ = {"link": stat.S_IFLNK, "file": stat.S_IFREG, "dir": stat.S_IFDIR}[kind]
                member.external_attr = (typ | entry["mode"]) << 16
                data = entry["text"].encode() if kind == "link" else regular_bytes(folder / rel) if kind == "file" else b""
                output.writestr(member, data)
        with zipfile.ZipFile(path) as check:
            require(check.testzip() is None and len(check.infolist()) == len(before), "ZIP verification failed")
            for rel, entry in before.items():
                member = check.getinfo(f"{folder.name}/{rel}" + ("/" if entry["kind"] == "dir" and rel else ""))
                data = check.read(member)
                require(stat.S_IMODE(member.external_attr >> 16) == entry["mode"], "ZIP permission mismatch")
                if entry["kind"] == "file":
                    require(hashlib.sha256(data).hexdigest() == entry["sha256"], "ZIP byte mismatch")
                elif entry["kind"] == "link":
                    require(data.decode() == entry["text"], "ZIP link mismatch")
        require(snapshot(folder) == before, f"Skill changed during backup: {folder}")
        return str(path.relative_to(root))
    except Exception:
        # Incomplete archives are preserved for diagnosis, never reported as a backup.
        raise


def link_text(path):
    if path.is_symlink():
        return os.readlink(path)
    require(not path.exists(), f"Foreign real entry preserved: {path}")
    return None


def render_catalog(root, state, config):
    lines = ["# Deployed Skills", "", "Generated by skills-army-hq; folders are the desired collection.", "",
             "| Skill | Description | Source / revision | Updated (UTC) | Digest | Targets |",
             "|---|---|---|---|---|---|"]
    def esc(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    # Rendering must not re-trip the missing-payload guard: the command path already ran it
    # (or is the remove that acknowledges one of several vanished payloads).
    found = inventory(root, state, list(state["skills"]))
    for name, info in found.items():
        receipt = state["skills"].get(name, {})
        statuses = []
        for target in config["targets"]:
            if target["enabled"]:
                path = location(target["path"]) / name
                good = path.is_symlink() and os.readlink(path) == str(root / name)
                statuses.append(f"{target['id']}: {'linked' if good else 'not linked'}")
        lines.append(f"| [{name}]({name}/SKILL.md) | {esc(info['description'])} | "
                     f"{esc(receipt.get('source', 'manual/unadopted'))} @ {receipt.get('commit') or 'no git'} | "
                     f"{receipt.get('updated', 'unknown')} | {info['digest']} | {esc('; '.join(statuses))} |")
    lines.extend(["", "## Runtime prerequisites", ""])
    for name, receipt in state["skills"].items():
        for prerequisite in receipt.get("prerequisites", []):
            lines.append(f"- {name}: {esc(prerequisite)}")
    return "\n".join(lines) + "\n"


def history(root, event):
    path = root / "changelog.md"
    old = regular_bytes(path).decode() if path.exists() else "# Deployment Changelog\n\n"
    require(old.startswith("# Deployment Changelog\n") and old.endswith("\n"), "Corrupt changelog header/tail")
    # Whole records have an end marker; interrupted writes are avoided by atomic replacement.
    records = old.split("\n## ")[1:]
    require(all(r.rstrip().endswith("<!-- end -->") for r in records), "Corrupt changelog record; recover before mutation")
    marker = f"<!-- operation:{event['id']} -->"
    if marker in old:
        return
    block = f"\n## {event['time']} — {event['kind']}\n{marker}\n\n"
    block += "```json\n" + json.dumps(event["details"], indent=2, sort_keys=True) + "\n```\n<!-- end -->\n"
    atomic_bytes(path, (old + block).encode())


def action_apply(root, action):
    kind = action["kind"]
    if kind == "payload":
        name = safe_name(action["name"])
        dest = root / name
        stage = root / ".staging" / action["op"]
        require(re.fullmatch(r"[a-f0-9]{32}", action["op"]) is not None, "Invalid stage receipt")
        location(stage)
        new, old = stage / "new", stage / "old"
        expected = action["after"]
        current = digest(dest) if dest.exists() else None
        require(not dest.is_symlink(), f"Payload replaced externally: {dest}")
        if current == expected:
            return
        require(current == action["before"] or current is None and old.exists(), f"Payload pre-state changed: {dest}")
        if old.exists():
            require(digest(old) == action["before"], "Prior staged payload changed")
        if expected is not None:
            require(digest(new) == expected, "Staged payload digest mismatch")
        if dest.exists():
            require(not old.exists(), "Existing prior payload staging; inspect recovery")
            dest.rename(old)
        if expected is not None:
            new.rename(dest)
    elif kind == "link":
        parent = location(action["root"])
        name = safe_name(action["name"]) if action["name"] not in ("intake.py", "sync.py") else action["name"]
        require(parent == root or not within(parent, root) and not within(root, parent), "Invalid link root")
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / name
        current = link_text(path)
        if current == action["after"]:
            return
        require(current == action["before"], f"Link pre-state changed; preserving: {path}")
        # One atomic replacement: a crash cannot leave a migrated link unlinked.
        # A pinned directory fd also keeps leaf writes out of a redirected parent.
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        temporary = ".deploy-skills-link-" + uuid.uuid4().hex
        try:
            location(parent)
            require(os.fstat(fd).st_ino == parent.stat().st_ino, "Target root changed")
            require(link_text(path) == current, "Link changed at mutation boundary")
            if action["after"] is None:
                os.unlink(name, dir_fd=fd)
            elif current is None:
                os.symlink(action["after"], name, dir_fd=fd)
            else:
                os.symlink(action["after"], temporary, dir_fd=fd)
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
            os.close(fd)
    elif kind == "retire-directory":
        parent = location(action["root"])
        path = parent / "skills-sync-trinity"
        stage = location(root / ".staging" / action["op"])
        old = stage / "retired"
        if not path.exists() and not path.is_symlink():
            require(digest(old) == action["before"], "Missing retired directory and backup staging")
            return
        require(digest(path) == action["before"], "Legacy directory changed; preserved")
        require(not old.exists(), "Legacy staging occupied")
        path.rename(old)
    else:
        raise DeployError(f"Unknown operation: {kind}")


def receipt_digest(receipt):
    return hashlib.sha256(json.dumps({k: v for k, v in receipt.items() if k != "checksum"},
                                    sort_keys=True).encode()).hexdigest()


def validate_receipt(root, receipt):
    require(receipt.get("schema") == SCHEMA and receipt.get("root") == str(root), "Invalid recovery receipt")
    require(receipt.get("checksum") == receipt_digest(receipt), "Corrupt recovery receipt checksum; inspect before mutation")
    validate_state(root, receipt["state"])
    validate_targets(root, receipt["targets"])
    require(isinstance(receipt.get("actions"), list), "Invalid recovery actions")
    for action in receipt["actions"]:
        kind = action.get("kind")
        if kind in ("payload", "retire-directory"):
            require(re.fullmatch(r"[a-f0-9]{32}", action.get("op", "")), "Invalid staged operation")
            name = safe_name(action["name"])
            require(kind != "retire-directory" or name == "skills-sync-trinity", "Invalid retirement")
            for key in ("before", "after"):
                value = action.get(key)
                require(value is None or isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
                        "Invalid payload digest")
        elif kind == "link":
            parent = location(action["root"])
            name = action["name"]
            if parent == root:
                require(name in ("intake.py", "sync.py") and action["before"] in
                        (None, f"deploy-skills/scripts/{name}", f"skills-army-hq/scripts/{name}")
                        and action["after"] == f"skills-army-hq/scripts/{name}", "Invalid manager entry")
            else:
                safe_name(name)
                require(not within(parent, root) and not within(root, parent), "Link root overlaps collection")
                require(action["after"] in (None, str(root / name)), "Unexpected link destination")
            require(action["before"] is None or isinstance(action["before"], str), "Invalid link pre-state")
        else:
            raise DeployError("Invalid recovery action kind")
    return receipt


def finish(root, receipt):
    validate_receipt(root, receipt)
    validate_history(root)
    for action in receipt["actions"]:
        action_apply(root, action)
    atomic_json(root / STATE, receipt["state"])
    atomic_json(root / "targets.json", receipt["targets"])
    atomic_bytes(root / "catalog.md", render_catalog(root, receipt["state"], receipt["targets"]).encode())
    # The collection's front door is a real copy, refreshed with other generated docs.
    readme = root / "skills-army-hq" / "README.md"
    if readme.exists():
        atomic_bytes(root / "README.md", regular_bytes(readme))
    history(root, receipt["event"])
    (root / PENDING).unlink()
    # Staging is retained (not silently recursively deleted); ZIPs are the user-facing backups.


def transact(root, state, config, actions, kind, details):
    receipt = {"schema": SCHEMA, "root": str(root), "state": state, "targets": config,
               "actions": actions, "event": {"id": uuid.uuid4().hex, "time": utc(), "kind": kind, "details": details}}
    receipt["checksum"] = receipt_digest(receipt)
    validate_receipt(root, receipt)
    validate_history(root)
    require(not (root / PENDING).exists(), "Pending operation exists; recover it first")
    atomic_json(root / PENDING, receipt)
    finish(root, receipt)


def validate_history(root):
    # Validate history before side effects, not only when appending at the end.
    require(not (root / "changelog.md").is_symlink(), "Changelog symlink refused")
    if (root / "changelog.md").exists():
        text = regular_bytes(root / "changelog.md").decode()
        require(text.startswith("# Deployment Changelog\n") and text.endswith("\n")
                and all(r.rstrip().endswith("<!-- end -->") for r in text.split("\n## ")[1:]),
                "Corrupt changelog; inspect before mutation")


def stage_payload(root, source, before, after, name=None, ignored=()):
    op = uuid.uuid4().hex
    stage = root / ".staging" / op
    location(stage)
    stage.mkdir(parents=True)
    if source is not None:
        source = Path(source)
        def exclude(current, names):
            return [item for item in names if (Path(current) == source and item in ignored)
                    or (item == ".git" and ".git" in ignored)]
        shutil.copytree(source, stage / "new", symlinks=True, ignore=exclude)
        require(digest(stage / "new") == after and digest(source, ignored) == after,
                "Source changed during staging")
    action_name = name or (source.name if source else "")
    return {"kind": "payload", "name": action_name, "op": op,
            "before": before, "after": after}


def default_root():
    return os.environ.get("XYZ_SKILLS_ROOT") or str(Path.home() / "git-pulse-sync" / "Deployed Skills")


def adopt_existing(root, apply):
    """Attach local receipts to a clean Git-carried collection without copying payloads."""
    if (root / STATE).exists():
        load(root)
        print("Collection already initialized")
        return
    local = [STATE, PENDING, "targets.json", "catalog.md", "changelog.md",
             ".deploy-skills.lock", "backups/", ".staging/"]
    require(not any((root / p).exists() or (root / p).is_symlink()
                    for p in (PENDING, "targets.json")), "Existing local state; inspect or recover before adoption")
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        require(result.returncode == 0, f"Cannot adopt collection ({' '.join(args)}): {result.stderr.strip()}")
        return result.stdout.strip()
    require(not git("ls-files", "--", *local), "Machine state is tracked; untrack it before adoption")
    for name in local:
        git("check-ignore", "--quiet", "--no-index", name + "probe" if name.endswith("/") else name)
    require(not git("status", "--porcelain", "--", "."), "Pulse collection must be clean before adoption")
    found = inventory(root, {"skills": {}})
    require("skills-army-hq" in found, "Collection lacks skills-army-hq")
    git("ls-files", "--error-unmatch", "--", *(f"{name}/SKILL.md" for name in found))
    for name in ("intake.py", "sync.py"):
        require(link_text(root / name) == f"skills-army-hq/scripts/{name}"
                and (root / name).is_file(), f"Missing or foreign manager entry: {name}")
    require(regular_bytes(root / "README.md") == regular_bytes(root / "skills-army-hq" / "README.md"),
            "Collection README differs from manager; refresh it with `update skills-army-hq` on any device, then publish")
    state = {"schema": SCHEMA, "root": str(root), "collection": uuid.uuid4().hex,
             "skills": {name: source_record(root / name)[1] for name in found}, "links": {}}
    validate_history(root)
    print(json.dumps({"operation": "adopt-existing", "root": str(root), "skills": sorted(found), "apply": apply}))
    if apply:
        with locked(root):
            require(not (root / STATE).exists() and not (root / "targets.json").exists(),
                    "Local state appeared during adoption; inspect before retrying")
            require(inventory(root, {"skills": {}}) == found, "Payload changed during adoption")
            transact(root, state, defaults(), [], "adopt-existing", {"skills": sorted(found)})


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=default_root(),
                   help="Collection root (env: XYZ_SKILLS_ROOT)")
    p.add_argument("--apply", action="store_true", help="Apply the requested mutation; default is preview")
    p.add_argument("--dry-run", action="store_true", help="Write nothing")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--adopt-existing", action="store_true", help="Attach local state to a clean Pulse collection in place")
    sub.add_parser("activate-manager", help="Switch legacy collection entry links to the installed Skills Army HQ")
    add = sub.add_parser("add"); add.add_argument("source")
    update = sub.add_parser("update"); update.add_argument("name"); update.add_argument("--source")
    remove = sub.add_parser("remove"); remove.add_argument("name"); remove.add_argument("--allow-empty", action="store_true")
    for verb in ("list", "catalog", "recover"):
        sub.add_parser(verb)
    target = sub.add_parser("targets")
    target.add_argument("--id"); target.add_argument("--path")
    target.add_argument("--consumer", action="append", default=[])
    target.add_argument("--disable", action="store_true"); target.add_argument("--remove", action="store_true")
    settings = sub.add_parser("settings", help="Device-wide settings: drift policy, canonical forge root, opt-in source rules")
    settings.add_argument("--drift", choices=("warn", "refuse"), help="refuse makes sync --apply refuse drifted skills")
    settings.add_argument("--canonical", metavar="FORGE_ROOT"); settings.add_argument("--no-canonical", action="store_true")
    settings.add_argument("--source-rule", choices=SOURCE_RULES, help="any (default), git, or clean")
    settings.add_argument("--source-repo", action="append", default=[], metavar="REPO_ROOT",
                          help="Allow only sources inside this repository (repeatable)")
    settings.add_argument("--no-source-repos", action="store_true")
    note = sub.add_parser("prerequisite"); note.add_argument("name"); note.add_argument("text")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = location(args.root)
        apply = args.apply and not args.dry_run
        if args.command == "init":
            if args.adopt_existing:
                adopt_existing(root, apply)
                return 0
            source = Path(__file__).resolve().parent.parent
            info, ignored = package_info(source)
            require((source / "scripts" / "sync.py").is_file(), "Manager is incomplete: missing scripts/sync.py")
            initial_digest = digest(source, ignored)
            require(not within(root, source) and not within(source, root) and root != source, "Source/collection overlap")
            if (root / STATE).exists():
                load(root)
                print("Collection already initialized")
                return 0
            require(not root.exists() or not any(root.iterdir()), f"Init requires empty folder: {root}")
            print(json.dumps({"operation": "init", "root": str(root), "manager": str(source), "apply": apply}))
            if not apply:
                return 0
            root.mkdir(parents=True, exist_ok=True)
            with locked(root):
                require(set(p.name for p in root.iterdir()) <= {".deploy-skills.lock"},
                        "Collection changed during init; inspect or recover")
                state = {"schema": SCHEMA, "root": str(root), "collection": uuid.uuid4().hex,
                         "skills": {"skills-army-hq": {**info, "source": str(source), "digest": initial_digest,
                                                      "updated": utc(), "prerequisites": []}}, "links": {}}
                action = stage_payload(root, source, None, initial_digest, info["name"], ignored)
                links = [{"kind": "link", "root": str(root), "name": f"{n}.py", "before": None,
                          "after": f"skills-army-hq/scripts/{n}.py"} for n in ("intake", "sync")]
                transact(root, state, defaults(), [action, *links], "init", {"manager_digest": initial_digest})
            return 0
        if args.command == "recover":
            receipt = validate_receipt(root, read_json(root / PENDING))
            print(json.dumps({"operation": "recover", "event": receipt.get("event"), "apply": apply}))
            if apply:
                with locked(root):
                    finish(root, read_json(root / PENDING))
            return 0
        state, config = load(root)
        if args.command == "list":
            print(json.dumps({"skills": inventory(root, state), "receipts": state["skills"], "targets": config}, indent=2))
            return 0
        if args.command == "targets" and not args.id:
            print(json.dumps(config, indent=2)); return 0
        settings_requested = args.command == "settings" and (
            args.drift or args.canonical or args.no_canonical or args.source_rule
            or args.source_repo or args.no_source_repos)
        if args.command == "settings" and not settings_requested:
            print(json.dumps({k: config.get(k) for k in ("drift", "canonical", "source_rules")}, indent=2)); return 0
        # Preview follows exactly the same validation path but never takes a write lock.
        with locked(root) if apply else contextlib.nullcontext():
            state, config = load(root)
            validate_history(root)
            # remove IS the acknowledgment path: several vanished payloads must not deadlock
            # each other (each remove still acknowledges exactly one; the rest keep blocking).
            found = inventory(root, state, list(state["skills"]) if args.command == "remove" else [])
            details, actions = {}, []
            if args.command == "activate-manager":
                require("skills-army-hq" in found, "Import skills-army-hq before activation")
                require(found["skills-army-hq"]["digest"] == state["skills"]["skills-army-hq"]["digest"],
                        "Manager differs from imported receipt")
                for name in ("intake.py", "sync.py"):
                    require((root / "skills-army-hq" / "scripts" / name).is_file(), "Incomplete manager")
                    actions.append({"kind": "link", "root": str(root), "name": name,
                                    "before": os.readlink(root / name), "after": f"skills-army-hq/scripts/{name}"})
                details = {"manager": "skills-army-hq", "actions": actions}
            elif args.command in ("add", "update"):
                raw = args.source if args.command == "add" else args.source or state["skills"].get(args.name, {}).get("source")
                require(raw, "No source receipt; provide --source")
                source, record, ignored = source_record(raw, config.get("source_rules"))
                name = record["name"]
                if args.command == "update":
                    require(name == safe_name(args.name) and name in found, "Update name/source mismatch or absent skill")
                require(not within(source, root) and not within(root, source) and source != root, "Source/collection overlap")
                require(name not in found or args.command == "update", f"Skill already exists: {name}; use update")
                before = found.get(name, {}).get("digest")
                if before == record["digest"]:
                    print(f"Unchanged: {name}"); return 0
                details = {"name": name, "source": str(source), "before": before, "after": record["digest"],
                           "commit": record.get("commit")}
                if apply:
                    if before:
                        details["backup"] = archive(root, root / name)
                    actions.append(stage_payload(root, source, before, record["digest"], name, ignored))
                record["prerequisites"] = state["skills"].get(name, {}).get("prerequisites", [])
                state["skills"][name] = record
            elif args.command == "remove":
                name = safe_name(args.name)
                require(name != "skills-army-hq", "Disable targets and sync before retiring the manager; keep its recovery tools available")
                require(not any(os.readlink(root / entry).startswith(name + "/")
                                for entry in ("intake.py", "sync.py")), "Cannot remove the active manager")
                require(name in state["skills"] or name in found, f"Unknown skill: {name}")
                require(len(found.keys() - {name}) or args.allow_empty, "Removing final skill requires --allow-empty")
                details = {"name": name, "before": found.get(name, {}).get("digest"), "links": "run sync apply to withdraw owned links"}
                if name in found and apply:
                    details["backup"] = archive(root, root / name)
                    action = stage_payload(root, None, details["before"], None); action["name"] = name
                    actions.append(action)
                state["skills"].pop(name, None)
            elif args.command == "targets":
                require(NAME.fullmatch(args.id), "Invalid target id")
                previous = next((t for t in config["targets"] if t["id"] == args.id), None)
                config["targets"] = [t for t in config["targets"] if t["id"] != args.id]
                if not args.remove:
                    require(args.path or previous, "New target needs --path")
                    config["targets"].append({"id": args.id, "path": args.path or previous["path"],
                                              "consumers": args.consumer or (previous or {}).get("consumers", []),
                                              "enabled": not args.disable})
                validate_targets(root, config)
                details = {"targets": config["targets"]}
            elif args.command == "settings":
                require(not (args.canonical and args.no_canonical), "Choose --canonical or --no-canonical")
                require(not (args.source_repo and args.no_source_repos), "Choose --source-repo or --no-source-repos")
                if args.drift:
                    config["drift"] = args.drift
                if args.no_canonical:
                    config.pop("canonical", None)
                elif args.canonical:
                    config["canonical"] = str(Path(args.canonical).expanduser().resolve(strict=True))
                rules = dict(config.get("source_rules", {}))
                if args.source_rule:
                    rules["mode"] = args.source_rule
                if args.no_source_repos:
                    rules.pop("repositories", None)
                for raw_repo in args.source_repo:
                    repo = str(Path(raw_repo).expanduser().resolve(strict=True))
                    if repo not in rules.setdefault("repositories", []):
                        rules["repositories"].append(repo)
                if rules.get("mode") == "any":
                    rules.pop("mode")
                if rules:
                    config["source_rules"] = rules
                else:
                    config.pop("source_rules", None)
                validate_targets(root, config)
                details = {"settings": {k: config.get(k) for k in ("drift", "canonical", "source_rules")}}
            elif args.command == "prerequisite":
                name = safe_name(args.name)
                require(name in state["skills"], "Skill must be adopted first")
                if args.text in state["skills"][name].get("prerequisites", []):
                    print("Prerequisite already recorded"); return 0
                state["skills"][name].setdefault("prerequisites", []).append(args.text)
                details = {"name": name, "prerequisite": args.text}
            elif args.command == "catalog":
                for name, info in found.items():
                    state["skills"].setdefault(name, {**info, "updated": utc(), "prerequisites": []})
                details = {"skills": sorted(found)}
            print(json.dumps({"operation": args.command, "apply": apply, **details}, indent=2))
            if apply:
                transact(root, state, config, actions, args.command, details)
        return 0
    except (DeployError, OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"skills-army-hq: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
