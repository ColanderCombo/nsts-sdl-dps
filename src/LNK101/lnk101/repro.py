"""Track git version info and MD5 checksums"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def get_git_info():
    src_dir = Path(__file__).parent
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=src_dir, stderr=subprocess.DEVNULL,
        ).decode().strip()
        date = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci", "HEAD"],
            cwd=src_dir, stderr=subprocess.DEVNULL,
        ).decode().strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=src_dir, stderr=subprocess.DEVNULL,
        ).decode().strip()
        if dirty:
            commit += " (dirty)"
        return {"commit": commit, "date": date}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"commit": "unknown", "date": "unknown"}


def version_string():
    info = get_git_info()
    commit = info["commit"].split()[0]  # strip " (dirty)" if present
    short = commit[:7] if commit != "unknown" else "unknown"
    date = info["date"].split()[0] if info["date"] != "unknown" else "unknown"
    dirty = " (dirty)" if "(dirty)" in info.get("commit", "") else ""
    return f"{short}({date}){dirty}"


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class ReproTracker:
    def __init__(self, tool_name, tool_version=""):
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.git_info = get_git_info()
        self.files = {}  # resolved path str -> {"md5": ..., "role": ...}

    def track(self, path, role="input"):
        resolved = str(Path(path).resolve())
        md5 = md5_file(path)
        self.files[resolved] = {"md5": md5, "role": role}
        return md5

    def to_dict(self):
        return {
            "tool": self.tool_name,
            "version": self.tool_version,
            "git": self.git_info,
            "files": {p: info for p, info in sorted(self.files.items())},
        }

    def print_summary(self, file=sys.stderr):
        git = self.git_info
        print(f"\n{self.tool_name} {self.tool_version}", file=file)
        print(f"  git commit: {git['commit']}", file=file)
        print(f"  git date:   {git['date']}", file=file)
        if self.files:
            print("  md5sums:", file=file)
            for path, info in sorted(self.files.items()):
                name = Path(path).name
                print(
                    f"    {info['md5']}  {name}  ({info['role']})",
                    file=file,
                )

    def save(self, path, extra=None):
        data = {"repro": self.to_dict()}
        if extra:
            data.update(extra)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


    def check(self, repro_path):
        with open(repro_path) as f:
            raw = json.load(f)

        saved = raw["repro"]

        issues = []

        # Git version
        saved_git = saved.get("git", {})
        if saved_git.get("commit") != self.git_info.get("commit"):
            issues.append(
                f"git commit differs:\n"
                f"    saved:   {saved_git.get('commit', '?')}\n"
                f"    current: {self.git_info.get('commit', '?')}"
            )

        # Files
        saved_files = saved.get("files", {})
        for path, saved_info in sorted(saved_files.items()):
            if path in self.files:
                if self.files[path]["md5"] != saved_info["md5"]:
                    issues.append(
                        f"CHANGED: {path}\n"
                        f"    saved:   {saved_info['md5']}\n"
                        f"    current: {self.files[path]['md5']}"
                    )
            else:
                # Not tracked in current run — check on disk
                p = Path(path)
                if p.exists():
                    current_md5 = md5_file(path)
                    if current_md5 != saved_info["md5"]:
                        issues.append(
                            f"CHANGED (not in current run): {path}\n"
                            f"    saved:   {saved_info['md5']}\n"
                            f"    current: {current_md5}"
                        )
                else:
                    issues.append(f"MISSING: {path} (present in saved run)")

        # Files in current run but not saved
        for path in sorted(self.files):
            if path not in saved_files:
                issues.append(f"NEW: {path} (not in saved run)")

        return issues

    def print_check(self, repro_path, file=sys.stderr):
        issues = self.check(repro_path)
        if issues:
            print(f"\n--check-repro: {len(issues)} difference(s):", file=file)
            for issue in issues:
                for line in issue.splitlines():
                    print(f"  {line}", file=file)
        else:
            print("\n--check-repro: all files match", file=file)
        return len(issues)
