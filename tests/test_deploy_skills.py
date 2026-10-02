"""Skills Army HQ behavioral tests (ported from XYZ-forge test/test_deploy_skills.py, GH-484/660;
source-agnostic intake per #3). Every write is confined to a validated temporary fixture, and no
forge checkout is needed: the drift checker is a stub fixture honouring its --json contract."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO  # this repository's root is the skills-army-hq bundle
STUB_CHECKER = REPO / "tests" / "fixtures" / "stub_skill_drift_check.py"
# Repo-only paths that are not part of the bundle a fixture installs.
REPO_ONLY = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".github", ".xyz", "tests", ".relay-scratch",
                                   ".DS_Store", "MANIFEST.txt", ".xyz-forge-revision", ".gitignore")
spec = importlib.util.spec_from_file_location("deploy_intake_test", BUNDLE / "scripts" / "intake.py")
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)


def tree(path):
    """Independent byte/link/mode/mtime observer, never follows symlinks."""
    result = {}
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            st = p.lstat()
            result[str(p.relative_to(path))] = (st.st_mode, st.st_mtime_ns,
                os.readlink(p) if p.is_symlink() else hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
    return result


class DeploySkillsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gh484-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.repo = self.work / "source repo"
        self.repo.mkdir()
        self.bundle = self.repo / "skills" / "skills-army-hq"
        shutil.copytree(BUNDLE, self.bundle, ignore=REPO_ONLY)
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture")
        self.root = self.work / "Documents" / "Deployed Skills"
        self.home = self.work / "alternate home"
        self.home.mkdir()
        self.target = self.work / "app skills"
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env.pop("XYZ_FORGE_ROOT", None)  # GH-660: never inherit the operator's canonical root into a fixture
        self.cli("--apply", "init")

    def git(self, *args):
        run = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def cli(self, *args, sync=False, code=0, copied=False):
        name = "sync.py" if sync else "intake.py"
        script = self.root / name if copied else self.bundle / "scripts" / name
        run = subprocess.run([sys.executable, "-B", str(script), "--root", str(self.root), *map(str, args)],
                             env=self.env, cwd=self.home, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, code, f"{args}\n{run.stdout}\n{run.stderr}")
        return run

    def source(self, name="sample"):
        folder = self.repo / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Fixture behavior.\n---\n\nRead only.\n")
        (folder / "run.py").write_text("print('fixture')\n")
        (folder / "run.py").chmod(0o755)
        (folder / "link.py").symlink_to("run.py")
        return folder

    def enable(self, target=None, ident="fixture"):
        self.cli("--apply", "targets", "--id", ident, "--path", target or self.target, "--consumer", "Fixture app")

    def state(self):
        return json.loads((self.root / intake.STATE).read_text())

    def pulse_fixture(self):
        pulse = self.repo / "Deployed Skills"
        pulse.mkdir()
        shutil.copytree(self.bundle, pulse / "skills-army-hq")
        shutil.copyfile(self.bundle / "README.md", pulse / "README.md")
        shutil.copytree(self.source(), pulse / "sample")
        for name in ("intake.py", "sync.py"):
            (pulse / name).symlink_to(f"skills-army-hq/scripts/{name}")
        (pulse / ".gitignore").write_text(".deploy-skills*\ntargets.json\ncatalog.md\nchangelog.md\nbackups/\n.staging/\n*.zip\n*.lock\n__pycache__/\n*.pyc\n")
        self.git("add", "Deployed Skills")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pulse payloads")
        self.root = pulse
        return pulse

    def test_pulse_adoption_two_devices_and_pull_readthrough(self):
        first = self.pulse_fixture()
        second_repo = self.work / "device-b" / "git-pulse-sync"
        run = subprocess.run(["git", "clone", "-q", str(self.repo), str(second_repo)], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        second = second_repo / "Deployed Skills"
        identities = []
        for i, root in enumerate((first, second)):
            self.root = root
            before = tree(root)
            self.cli("init", "--adopt-existing", copied=True)
            self.assertEqual(tree(root), before)
            payload = tree(root / "sample")
            self.cli("--apply", "init", "--adopt-existing", copied=True)
            self.assertEqual(tree(root / "sample"), payload)
            identities.append(self.state()["collection"])
            self.assertEqual(set(self.state()["skills"]), {"skills-army-hq", "sample"})
            self.assertFalse(any(t["enabled"] for t in json.loads((root / "targets.json").read_text())["targets"]))
            self.enable(self.work / f"app-{i}")
            self.cli("--apply", sync=True, copied=True)
            link = self.work / f"app-{i}" / "sample"
            self.assertEqual(link.resolve(), root / "sample")
            before = tree(root)
            self.cli("--apply", "init", "--adopt-existing", copied=True)
            self.assertEqual(tree(root), before)
            run = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "."], capture_output=True, text=True)
            self.assertEqual((run.returncode, run.stdout), (0, ""), run.stderr)
        self.assertNotEqual(*identities)
        (first / "sample" / "run.py").write_text("print('updated upstream')\n")
        self.git("add", "Deployed Skills/sample/run.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "publish update")
        run = subprocess.run(["git", "-C", str(second_repo), "pull", "--ff-only"], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual((self.work / "app-1" / "sample" / "run.py").read_bytes(), (first / "sample" / "run.py").read_bytes())
        self.assertEqual(self.state()["collection"], identities[1])

    def test_pulse_adoption_refuses_unprotected_or_foreign_state(self):
        root = self.pulse_fixture()
        ignore = root / ".gitignore"
        original = ignore.read_text()
        ignore.write_text(original.replace("targets.json\n", ""))
        before = tree(root)
        run = self.cli("--apply", "init", "--adopt-existing", copied=True, code=2)
        self.assertIn("Cannot adopt collection", run.stderr)
        self.assertEqual(tree(root), before)
        ignore.write_text(original)
        (root / "targets.json").write_text("{}\n")
        before = tree(root)
        run = self.cli("--apply", "init", "--adopt-existing", copied=True, code=2)
        self.assertIn("Existing local state", run.stderr)
        self.assertEqual(tree(root), before)
        (root / "targets.json").unlink()
        self.git("add", "-f", "Deployed Skills/.gitignore")
        (root / "catalog.md").write_text("foreign tracked catalog\n")
        self.git("add", "-f", "Deployed Skills/catalog.md")
        before = tree(root)
        run = self.cli("--apply", "init", "--adopt-existing", copied=True, code=2)
        self.assertIn("Machine state is tracked", run.stderr)
        self.assertEqual(tree(root), before)

    def test_pulse_default_root(self):
        env = {**self.env}
        env.pop("XYZ_SKILLS_ROOT", None)
        for name, args in (("intake.py", ["init"]), ("sync.py", ["--status"])):
            run = subprocess.run([sys.executable, str(self.bundle / "scripts" / name), *args],
                                 env=env, capture_output=True, text=True)
            self.assertIn(str(self.home / "git-pulse-sync" / "Deployed Skills"), run.stdout + run.stderr)
        with patch.dict(os.environ, {"XYZ_SKILLS_ROOT": str(self.root)}):
            self.assertEqual(intake.default_root(), str(self.root))

    def test_a1_copied_manager_and_no_source_dependency(self):
        self.repo.rename(self.work / "source hidden")
        self.cli("list", copied=True)
        self.cli("--apply", "catalog", copied=True)
        self.cli("--status", sync=True, copied=True)
        self.assertTrue((self.root / "skills-army-hq" / "SKILL.md").is_file())
        self.assertEqual(len(list((self.root / "skills-army-hq").rglob("*.py"))), 2)

    def test_a1_incomplete_manager_refused(self):
        (self.bundle / "scripts" / "sync.py").unlink()
        old = tree(self.work)
        self.cli("init", code=2)
        self.assertEqual(tree(self.work), old)

    def test_a1_failed_init_preserved_then_retry_after_backup(self):
        fresh = self.work / "fresh collection"
        with patch.object(intake.shutil, "copytree", side_effect=OSError("init copy failure")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(intake.main(["--root", str(fresh), "--apply", "init"]), 2)
        self.assertFalse((fresh / intake.STATE).exists())
        self.assertFalse((fresh / intake.PENDING).exists())
        before = tree(fresh)
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(intake.main(["--root", str(fresh), "--apply", "init"]), 2)
        self.assertEqual(tree(fresh), before)
        backup = self.work / "failed init backup"
        fresh.rename(backup)
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(intake.main(["--root", str(fresh), "--apply", "init"]), 0)
        self.assertEqual(tree(backup), before)
        self.assertTrue((fresh / "skills-army-hq" / "SKILL.md").is_file())

    def test_a1_legacy_manager_activation_preserves_collection(self):
        manager = self.root / "skills-army-hq"
        legacy = self.root / "deploy-skills"
        manager.rename(legacy)
        skill = legacy / "SKILL.md"
        skill.write_text(skill.read_text().replace("name: skills-army-hq", "name: deploy-skills"))
        state = self.state()
        identity = state["collection"]
        record = state["skills"].pop("skills-army-hq")
        record.update(name="deploy-skills", digest=intake.digest(legacy))
        state["skills"]["deploy-skills"] = record
        intake.atomic_json(self.root / intake.STATE, state)
        for name in ("intake.py", "sync.py"):
            (self.root / name).unlink()
            (self.root / name).symlink_to(f"deploy-skills/scripts/{name}")
        self.cli("--apply", "remove", "deploy-skills", code=2)
        self.cli("--apply", "add", self.bundle)
        before = tree(self.root)
        self.cli("activate-manager")
        self.assertEqual(tree(self.root), before)
        self.cli("--apply", "activate-manager")
        self.cli("--apply", "remove", "deploy-skills", copied=True)
        self.cli("list", copied=True)
        self.assertEqual(self.state()["collection"], identity)
        self.assertFalse(legacy.exists())
        self.assertTrue(list((self.root / "backups").glob("deploy-skills-*.zip")))
        for name in ("intake.py", "sync.py"):
            self.assertEqual(os.readlink(self.root / name), f"skills-army-hq/scripts/{name}")

    def test_a1_readme_copied_updated_and_archived(self):
        source = self.bundle / "README.md"
        deployed = self.root / "skills-army-hq" / "README.md"
        landing = self.root / "README.md"
        original = source.read_bytes()
        self.assertTrue(original)
        self.assertEqual(deployed.read_bytes(), original)
        self.assertFalse(landing.is_symlink())
        self.assertEqual(landing.read_bytes(), original)
        revised = original + b"\nFixture provenance update.\n"
        source.write_bytes(revised)
        self.cli("update", "skills-army-hq")
        self.assertEqual(deployed.read_bytes(), original)
        self.assertEqual(landing.read_bytes(), original)
        self.cli("--apply", "update", "skills-army-hq")
        self.assertEqual(deployed.read_bytes(), revised)
        self.assertEqual(landing.read_bytes(), revised)
        archives = list((self.root / "backups").glob("skills-army-hq-*.zip"))
        self.assertEqual(len(archives), 1)
        with zipfile.ZipFile(archives[0]) as archive:
            self.assertEqual(archive.read("skills-army-hq/README.md"), original)
        self.repo.rename(self.work / "source hidden")
        self.assertEqual(deployed.read_bytes(), revised)
        landing.unlink()
        self.cli("--apply", "catalog", copied=True)
        self.assertEqual(landing.read_bytes(), revised)
        landing.write_bytes(b"corrupted landing copy")
        with self.assertRaises(AssertionError):
            self.assertEqual(landing.read_bytes(), revised)
        deployed.write_bytes(b"corrupted copy")
        with self.assertRaises(AssertionError):
            self.assertEqual(deployed.read_bytes(), revised)

    def test_a2_archives_round_trip_and_same_day_collisions(self):
        source = self.source()
        self.cli("--apply", "add", source)
        expected = intake.snapshot(self.root / "sample")
        for version in (2, 3):
            (source / "run.py").write_text(f"print({version})\n")
            self.cli("--apply", "update", "sample")
        archives = sorted((self.root / "backups").glob("sample-*.zip"))
        self.assertEqual(len(archives), 2)
        # The collision suffix follows the full date; a bare "-02.zip" check misfires on the 2nd of a month.
        collision = lambda p: p.stem.count("-") == 4  # sample-YYYY-MM-DD-02 vs sample-YYYY-MM-DD
        self.assertTrue(any(collision(p) and p.stem.endswith("-02") for p in archives))
        first = next(p for p in archives if not collision(p))
        restored = self.work / "restore"
        restored.mkdir()
        with zipfile.ZipFile(first) as z:
            self.assertIsNone(z.testzip())
            for member in z.infolist():
                p = restored / member.filename
                self.assertTrue(p.resolve().is_relative_to(restored))
                mode = member.external_attr >> 16
                if stat.S_ISDIR(mode):
                    p.mkdir(parents=True, exist_ok=True)
                elif stat.S_ISLNK(mode):
                    p.symlink_to(z.read(member).decode())
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(z.read(member))
                if not stat.S_ISLNK(mode):
                    p.chmod(stat.S_IMODE(mode))
        self.assertEqual(intake.snapshot(restored / "sample"), expected)
        self.cli("--apply", "remove", "sample")
        self.assertFalse((self.root / "sample").exists())
        self.assertEqual(len(list((self.root / "backups").glob("sample-*.zip"))), 3)

    def test_a2_archive_crc_and_copy_failure_preserve_live_payload(self):
        source = self.source()
        self.cli("--apply", "add", source)
        self.enable(); self.cli("--apply", sync=True)
        old = intake.snapshot(self.root / "sample")
        (source / "run.py").write_text("changed\n")
        for target, kwargs in (("testzip", {"return_value": "bad"}),):
            with patch.object(intake.zipfile.ZipFile, target, **kwargs), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(intake.main(["--root", str(self.root), "--apply", "update", "sample"]), 2)
        with patch.object(intake.shutil, "copytree", side_effect=OSError("injected copy failure")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(intake.main(["--root", str(self.root), "--apply", "update", "sample"]), 2)
        self.assertEqual(intake.snapshot(self.root / "sample"), old)
        self.assertEqual(os.readlink(self.target / "sample"), str(self.root / "sample"))
        self.assertFalse((self.root / intake.PENDING).exists())

    def test_a2_self_update_preserves_runnable_manager(self):
        (self.bundle / "SKILL.md").write_text((self.bundle / "SKILL.md").read_text() + "\nFixture update.\n")
        self.cli("--apply", "update", "skills-army-hq")
        self.cli("list", copied=True)
        self.cli("--status", sync=True, copied=True)
        self.assertEqual(len(list((self.root / "backups").glob("skills-army-hq-*.zip"))), 1)

    def test_a3_previews_write_nothing(self):
        source = self.source()
        old = tree(self.work)
        self.cli("add", source)
        self.cli("--apply", "--dry-run", "add", source)
        self.cli("catalog")
        self.cli("targets", "--id", "fixture", "--path", self.target)
        self.cli(sync=True)
        self.cli("--apply", "--dry-run", sync=True)
        self.assertEqual(tree(self.work), old)
        new = self.work / "new collection"
        run = subprocess.run([sys.executable, str(self.bundle / "scripts" / "intake.py"), "--root", str(new), "init"], env=self.env, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(new.exists())

    def test_a3_names_malformed_sources_and_symlinks(self):
        source = self.source()
        original = (source / "SKILL.md").read_text()
        for content in ("", "---\nname: ../outside\ndescription: bad\n---\n", original.replace("name: sample", "name: SAMPLE"), original.replace("description: Fixture behavior.", "description:")):
            (source / "SKILL.md").write_text(content)
            self.cli("--apply", "add", source, code=2)
            self.assertFalse((self.root / "sample").exists())
        (source / "SKILL.md").write_text(original)
        for destination in (str(self.home), "../../outside", "missing", "."):
            link = source / "invalid"
            link.symlink_to(destination)
            self.cli("--apply", "add", source, code=2)
            link.unlink()
        a, b = source / "a", source / "b"
        a.mkdir(); b.mkdir()
        (a / "to-b").symlink_to("../b"); (b / "to-a").symlink_to("../a")
        self.cli("--apply", "add", source, code=2)

    def test_a3_overlap_alias_and_denied_scan(self):
        self.cli("--apply", "targets", "--id", "bad", "--path", self.root / "nested", code=2)
        self.cli("--apply", "targets", "--id", "bad", "--path", self.root.parent, code=2)
        alias = self.work / "alias"
        alias.symlink_to(self.home)
        self.cli("--apply", "targets", "--id", "bad", "--path", alias / "skills", code=2)
        with patch.object(intake.os, "walk", side_effect=PermissionError("denied scan")):
            with self.assertRaises(PermissionError):
                intake.inventory(self.root, self.state())

    def test_a4_owned_links_idempotence_removed_disabled_and_dedup(self):
        self.cli("--apply", "add", self.source())
        self.enable(); self.enable(ident="same-physical-root")
        first = json.loads(self.cli("--apply", sync=True).stdout)
        self.assertEqual(len(first["actions"]), 2)
        self.assertEqual(set(p.name for p in self.target.iterdir()), {"skills-army-hq", "sample"})
        old = (self.root / "changelog.md").read_bytes()
        self.assertEqual(json.loads(self.cli("--apply", sync=True).stdout)["actions"], [])
        self.assertEqual((self.root / "changelog.md").read_bytes(), old)
        (self.target / "sample").unlink()
        self.cli("--apply", sync=True)
        self.assertTrue((self.target / "sample").is_symlink())
        self.cli("--apply", "targets", "--id", "fixture", "--remove")
        self.cli("--apply", "targets", "--id", "same-physical-root", "--disable")
        self.cli("--apply", sync=True)
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(self.state()["links"], {})

    def test_a4_foreign_real_link_and_lost_ownership_preserved(self):
        self.cli("--apply", "add", self.source())
        self.enable(); self.cli("--apply", sync=True)
        (self.target / "sample").unlink()
        (self.target / "sample").symlink_to("foreign-dangling")
        (self.target / "skills-army-hq").unlink()
        (self.target / "skills-army-hq").mkdir()
        old = tree(self.target)
        self.cli("--apply", "remove", "sample")
        self.cli("--apply", sync=True, code=2)
        self.assertEqual(tree(self.target), old)
        self.assertEqual(len(self.state()["links"]), 2)

    def test_a4_adopt_and_selected_source_migration(self):
        source = self.source()
        self.cli("--apply", "add", source)
        self.enable(); self.target.mkdir()
        (self.target / "sample").symlink_to(source)
        (self.target / "skills-army-hq").symlink_to(self.root / "skills-army-hq")
        self.cli(sync=True, code=2)
        self.cli("--apply", "--migrate", "sample", "--adopt", "skills-army-hq", sync=True)
        self.assertEqual(os.readlink(self.target / "sample"), str(self.root / "sample"))
        self.assertEqual(self.state()["links"][str(self.target / "sample")]["previous"], str(source))

    def test_a4_partial_sync_reports_failure_and_keeps_success(self):
        self.enable(); self.target.mkdir()
        (self.target / "skills-army-hq").mkdir()
        other = self.work / "other app"
        self.enable(other, "other")
        run = self.cli("--apply", sync=True, code=2)
        result = json.loads(run.stdout)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["actions"]), 1)
        self.assertTrue((other / "skills-army-hq").is_symlink())
        self.assertIn("sync-partial", (self.root / "changelog.md").read_text())

    def test_a4_alternative_source_requires_exact_explicit_selection(self):
        source = self.source()
        self.cli("--apply", "add", source)
        prior = self.repo / "alternate" / "sample"
        prior.parent.mkdir(); shutil.copytree(source, prior, symlinks=True)
        (prior / "run.py").write_text("different version\n")
        self.enable(); self.target.mkdir()
        (self.target / "sample").symlink_to(prior)
        self.cli("--apply", "--migrate", "sample", sync=True, code=2)
        self.assertEqual(os.readlink(self.target / "sample"), str(prior))
        self.cli("--apply", "--migrate-from", f"sample={source}", sync=True, code=2)
        self.assertEqual(os.readlink(self.target / "sample"), str(prior))
        self.cli("--apply", "--migrate-from", f"sample={prior}", sync=True)
        self.assertEqual(os.readlink(self.target / "sample"), str(self.root / "sample"))
        self.assertEqual((prior / "run.py").read_text(), "different version\n")

    def test_a7_missing_manager_entry_blocks_usability(self):
        (self.root / "sync.py").unlink()
        before = tree(self.work)
        self.cli("--status", sync=True, code=2)
        self.assertEqual(tree(self.work), before)

    def test_a5_missing_payload_corrupt_state_and_history_refuse_prune(self):
        source = self.source()
        self.cli("--apply", "add", source); self.enable(); self.cli("--apply", sync=True)
        (self.root / "sample").rename(self.work / "manual withdrawal")
        before = tree(self.target)
        self.cli("--apply", sync=True, code=2)
        self.assertEqual(tree(self.target), before)
        self.cli("--apply", "remove", "sample")
        self.cli("--apply", sync=True)
        self.assertFalse((self.target / "sample").is_symlink())
        for control in (intake.STATE, "targets.json", "changelog.md"):
            file = self.root / control
            old = file.read_bytes()
            file.write_text("corrupt\n")
            before = tree(self.target)
            self.cli("--apply", sync=True, code=2)
            self.assertEqual(tree(self.target), before)
            file.write_bytes(old)

    def test_a5_two_missing_payloads_do_not_deadlock_remove(self):
        self.cli("--apply", "add", self.source("alpha")); self.cli("--apply", "add", self.source("beta"))
        for name in ("alpha", "beta"):
            (self.root / name).rename(self.work / f"vanished-{name}")
        self.cli("--apply", "catalog", code=2)
        self.cli("--apply", "remove", "alpha")
        self.cli("--apply", "catalog", code=2)
        self.cli("--apply", "remove", "beta")
        self.cli("--apply", "catalog")

        with intake.locked(self.root):
            before = (self.root / ".deploy-skills.lock").read_bytes()
            self.cli("--apply", sync=True, code=2)
            self.cli("--apply", "catalog", code=2)
            self.assertEqual((self.root / ".deploy-skills.lock").read_bytes(), before)
        self.cli("--apply", "catalog")

    def crash(self, point, args, sync=False):
        script = self.bundle / "scripts" / ("sync.py" if sync else "intake.py")
        code = '''import importlib.util,os,sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location("crash_cli",sys.argv[1]); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
shared=mod.shared if hasattr(mod,"shared") else mod
point=sys.argv[2]
if point == "rename":
 old=shared.Path.rename
 def wrapped(self,*a,**kw):
  result=old(self,*a,**kw)
  if self.name == "sample": os._exit(77)
  return result
 shared.Path.rename=wrapped
else:
 old=getattr(shared,point)
 def wrapped(*a,**kw):
  result=old(*a,**kw)
  if point != "atomic_json" or a[0].name == shared.STATE: os._exit(77)
  return result
 setattr(shared,point,wrapped)
raise SystemExit(mod.main(sys.argv[3:]))
'''
        run = subprocess.run([sys.executable, "-B", "-c", code, str(script), point, "--root", str(self.root), "--apply", *args],
                             env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 77, run.stderr)
        self.assertTrue((self.root / intake.PENDING).is_file())

    def test_a5_crash_each_publish_boundary_recovers_once(self):
        source = self.source()
        self.cli("--apply", "add", source)
        for point in ("rename", "action_apply", "atomic_json", "history"):
            with self.subTest(point=point):
                (source / "run.py").write_text(f"version {point}\n")
                self.crash(point, ["update", "sample"])
                self.cli("--apply", "catalog", code=2)
                self.cli("recover")
                receipt = json.loads((self.root / intake.PENDING).read_text())
                self.cli("--apply", "recover")
                self.assertEqual(intake.snapshot(self.root / "sample"), intake.snapshot(source))
                self.assertEqual((self.root / "changelog.md").read_text().count("operation:" + receipt["event"]["id"]), 1)
                self.assertFalse((self.root / intake.PENDING).exists())

    def test_a5_link_crash_and_corrupt_pending_fail_closed(self):
        self.enable()
        self.crash("action_apply", [], sync=True)
        pending = self.root / intake.PENDING
        good = pending.read_bytes()
        bad = json.loads(good); bad["actions"][0]["after"] = "/foreign"
        pending.write_text(json.dumps(bad))
        old = tree(self.target)
        self.cli("--apply", "recover", code=2)
        self.assertEqual(tree(self.target), old)
        pending.write_bytes(good)
        self.cli("--apply", "recover")
        self.assertEqual(os.readlink(self.target / "skills-army-hq"), str(self.root / "skills-army-hq"))

    def test_a10_retirement_is_explicit_and_recoverable(self):
        legacy = self.source("skills-sync-trinity")
        self.enable(); self.cli("--apply", sync=True)
        entry = self.target / legacy.name
        entry.symlink_to(legacy)
        old = tree(self.work)
        self.cli("--retire-trinity", legacy, sync=True)
        self.assertEqual(tree(self.work), old)
        self.cli("--apply", "--retire-trinity", legacy, sync=True)
        self.assertFalse(entry.is_symlink())
        self.assertIn(str(legacy), (self.root / "changelog.md").read_text())
        shutil.copytree(legacy, entry, symlinks=True)
        self.cli("--apply", "--retire-trinity", legacy, sync=True, code=2)
        self.assertTrue(entry.is_dir())
        self.cli("--apply", "--retire-trinity", legacy, "--archive-legacy", sync=True)
        self.assertFalse(entry.exists())
        self.assertEqual(len(list((self.root / "backups").glob("skills-sync-trinity-*.zip"))), 1)

    def test_a10_old_discoverable_skill_is_absent(self):
        self.assertFalse(list(REPO.glob("skills/*/skills-sync-trinity/SKILL.md")))  # any tier (GH-744)
        self.assertTrue((BUNDLE / "SKILL.md").is_file())

    # GH-660 drift check, made advisory by #3: sync runs the forge checker when a canonical root
    # resolves and WARNS; it refuses to deploy only on a device that opted in (settings --drift refuse).
    def forge(self):
        """A minimal canonical forge: skills/<name> + the stub checker, inside the fixture repo."""
        checker = self.repo / "utils" / "py" / "skill_drift_check.py"
        checker.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(STUB_CHECKER, checker)
        return self.repo

    def test_gh660_no_canonical_is_a_warning_not_a_pass(self):
        self.cli("--apply", "add", self.source()); self.enable()
        out = json.loads(self.cli("--apply", sync=True).stdout)
        self.assertIsNone(out["drift"])
        self.assertTrue(any("drift check skipped" in w for w in out["warnings"]))

    def test_drift_warns_by_default_and_refuses_only_when_opted_in(self):
        self.cli("--apply", "add", self.source()); self.enable()
        forge = self.forge()
        self.env["XYZ_FORGE_ROOT"] = str(forge)
        clean = json.loads(self.cli("--status", sync=True).stdout)
        self.assertEqual(clean["drift"]["drifted"], []); self.assertIn("sample", clean["drift"]["ok"])
        self.assertEqual(clean["drift"]["origin"], "XYZ_FORGE_ROOT")
        (self.root / "sample" / "SKILL.md").open("a").write("\nlocal hack\n")
        warned = json.loads(self.cli("--apply", sync=True).stdout)
        self.assertEqual([e["skill"] for e in warned["drift"]["drifted"]], ["sample"])
        self.assertTrue(any(w.startswith("DRIFTED sample") for w in warned["warnings"]))
        self.assertTrue(any("settings --drift refuse" in w for w in warned["warnings"]))
        self.assertTrue((self.target / "sample").is_symlink(), "default: drift is a warning and the deploy proceeds")
        # Opted in: while drift remains, a newly added skill must not be linked.
        self.cli("--apply", "settings", "--drift", "refuse")
        self.cli("--apply", "add", self.source("second"))
        preview = json.loads(self.cli(sync=True).stdout)  # preview tells the truth about --apply
        self.assertTrue(any(w.startswith("--apply would be REFUSED") for w in preview["warnings"]))
        refused = self.cli("--apply", sync=True, code=2)
        self.assertIn("REFUSED", refused.stderr); self.assertIn("DRIFTED sample", refused.stderr)
        self.assertIn(str(forge / "skills"), refused.stderr)
        self.assertFalse((self.target / "second").exists(), "refusal must deploy nothing")
        allowed = json.loads(self.cli("--apply", "--allow-drift", sync=True).stdout)
        self.assertTrue(any(w.startswith("--allow-drift") for w in allowed["warnings"]))
        self.assertTrue((self.target / "second").is_symlink())
        receipt = (self.root / "changelog.md").read_text()
        self.assertIn("--allow-drift", receipt, "override must leave a drift receipt in the changelog")
        self.assertIn(str(forge / "skills" / "sample" / "SKILL.md"), receipt)

    def test_gh660_collection_only_skill_is_unrecognized_never_refused(self):
        self.cli("--apply", "add", self.source("collection-only")); self.enable()
        forge = self.forge()
        shutil.rmtree(forge / "skills" / "collection-only")  # vendored, but the forge never owned it
        self.env["XYZ_FORGE_ROOT"] = str(forge)
        self.cli("--apply", "settings", "--drift", "refuse")
        out = json.loads(self.cli("--apply", sync=True).stdout)
        self.assertEqual(out["drift"]["unrecognized"], ["collection-only"]); self.assertEqual(out["drift"]["drifted"], [])

    def test_gh660_explicit_bad_canonical_is_an_error(self):
        self.cli("--apply", "add", self.source())
        run = self.cli("--status", "--canonical", str(self.work / "nowhere"), sync=True, code=2)
        self.assertIn("Canonical root from --canonical lacks", run.stderr)

    def test_settings_sets_canonical_without_hand_edits(self):
        self.cli("--apply", "add", self.source()); self.enable()
        forge = self.forge()
        cfg = self.root / "targets.json"
        before = tree(self.root)
        self.cli("settings", "--canonical", forge)
        self.assertEqual(tree(self.root), before, "settings preview must write nothing")
        self.cli("--apply", "settings", "--canonical", forge)
        self.assertEqual(json.loads(cfg.read_text())["canonical"], str(forge.resolve()))
        out = json.loads(self.cli("--status", sync=True).stdout)
        self.assertEqual(out["drift"]["origin"], 'targets.json "canonical"')
        # another intake write rewrites targets.json; the setting must survive and still resolve
        self.cli("--apply", "targets", "--id", "second", "--path", self.work / "second app", "--consumer", "Fixture 2")
        self.assertEqual(json.loads(cfg.read_text())["canonical"], str(forge.resolve()))
        self.cli("--apply", "settings", "--no-canonical")
        self.assertNotIn("canonical", json.loads(cfg.read_text()))
        self.assertIn("— settings", (self.root / "changelog.md").read_text())

    # #3: any folder may be a source. The safeguards are the receipts and history of what is copied
    # into the fleet folder and what is linked into each app.
    def loose(self, folder_name="downloaded copy", name="loose-skill"):
        folder = self.work / "loose" / folder_name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Loose fixture.\n---\n\nBody.\n")
        return folder

    def test_non_git_folder_with_any_name_is_added_and_every_change_recorded(self):
        folder = self.loose()
        self.cli("--apply", "add", folder)
        self.assertTrue((self.root / "loose-skill" / "SKILL.md").is_file())
        receipt = self.state()["skills"]["loose-skill"]
        self.assertEqual(receipt["source"], str(folder.resolve()))
        self.assertRegex(receipt["digest"], "^[a-f0-9]{64}$"); self.assertIn("updated", receipt)
        for key in ("repository", "commit", "branch", "dirty"):
            self.assertNotIn(key, receipt)
        self.assertIn("— add", (self.root / "changelog.md").read_text())
        self.assertIn("no git", (self.root / "catalog.md").read_text())
        (folder / "SKILL.md").write_text("---\nname: loose-skill\ndescription: Loose fixture v2.\n---\n\nBody.\n")
        self.cli("--apply", "update", "loose-skill")  # recorded source, still not a repo
        self.assertIn("v2", (self.root / "loose-skill" / "SKILL.md").read_text())
        self.enable(); self.cli("--apply", sync=True)
        self.assertTrue((self.target / "loose-skill").is_symlink())
        self.cli("--apply", "remove", "loose-skill"); self.cli("--apply", sync=True)
        self.assertFalse((self.target / "loose-skill").exists() or (self.target / "loose-skill").is_symlink())
        log = (self.root / "changelog.md").read_text()
        for kind in ("— add", "— update", "— remove"):
            self.assertIn(kind, log)
        self.assertGreaterEqual(log.count("— sync"), 2, "link and withdrawal are both recorded")

    def test_git_source_receipt_adds_repository_commit_branch_and_dirty(self):
        self.cli("--apply", "add", self.source())
        receipt = self.state()["skills"]["sample"]
        self.assertEqual(receipt["repository"], str(self.repo.resolve()))
        self.assertTrue(receipt["commit"]); self.assertTrue(receipt["branch"])
        self.assertTrue(receipt["dirty"], "untracked fixture files make the source dirty")

    def test_source_rules_are_opt_in_per_device(self):
        loose, tracked = self.loose(), self.source()
        self.assertIsNone(json.loads(self.cli("settings").stdout)["source_rules"], "no rules by default")
        self.cli("--apply", "settings", "--source-rule", "git")
        self.assertIn("Source rule 'git'", self.cli("--apply", "add", loose, code=2).stderr)
        self.cli("--apply", "settings", "--source-rule", "clean")
        self.assertIn("uncommitted", self.cli("--apply", "add", tracked, code=2).stderr)
        self.git("add", "skills/sample")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "sample")
        self.cli("--apply", "add", tracked)
        other = self.work / "other repo"; other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        self.cli("--apply", "settings", "--source-rule", "any", "--source-repo", other)
        self.assertIn("outside the allowed repositories", self.cli("--apply", "add", loose, code=2).stderr)
        self.cli("--apply", "settings", "--no-source-repos")
        self.cli("--apply", "add", loose)
        self.assertNotIn("source_rules", json.loads((self.root / "targets.json").read_text()))

    def test_git_dir_never_copied_and_bundle_root_leaves_repo_files(self):
        own_repo = self.loose("own repo", "self-contained")
        subprocess.run(["git", "-C", str(own_repo), "init", "-q"], check=True)
        nested = own_repo / "sub"
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "tests").mkdir(); (nested / "tests" / "keep.txt").write_text("nested tests stay\n")
        self.cli("--apply", "add", own_repo)
        self.assertFalse((self.root / "self-contained" / ".git").exists())
        self.assertFalse((self.root / "self-contained" / "sub" / ".git").exists(), ".git is never copied at any depth")
        self.assertTrue((self.root / "self-contained" / "sub" / "tests" / "keep.txt").is_file())
        bundle = self.loose("Some-Bundle-Repo", "bundled")
        (bundle / "MANIFEST.txt").write_text("SKILL.md\nscripts/run.py\n")
        (bundle / "scripts").mkdir(); (bundle / "scripts" / "run.py").write_text("print(1)\n")
        (bundle / "tests").mkdir(); (bundle / "tests" / "test_x.py").write_text("x\n")
        (bundle / "scripts" / "tests").mkdir(); (bundle / "scripts" / "tests" / "fixture.txt").write_text("kept\n")
        (bundle / "LICENSE").write_text("license\n")
        self.cli("--apply", "add", bundle)
        copied = self.root / "bundled"
        self.assertTrue((copied / "scripts" / "run.py").is_file())
        self.assertTrue((copied / "scripts" / "tests" / "fixture.txt").is_file(), "repo-only ignores apply at the root only")
        for repo_only in ("tests", "LICENSE", "MANIFEST.txt"):
            self.assertFalse((copied / repo_only).exists(), repo_only)
        (bundle / "MANIFEST.txt").write_text("README.md\n")
        self.assertIn("does not list SKILL.md", self.cli("--apply", "update", "bundled", code=2).stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
