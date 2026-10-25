"""Immutable Git context and strict diff-side anchors, never guessed prose locations."""
import re

from .common import ApiError, git


def safe_path(path):
    if (not isinstance(path, str) or not path or "\0" in path or path.startswith(("/", "-"))
            or ".." in path.split("/")):
        raise ApiError("invalid_anchor", "Invalid repository-relative path.")
    return path


class GitContext:
    def __init__(self, manifest):
        self.common = manifest["git_common_dir"]
        self.head = manifest.get("snapshot", {}).get("head", "")
        self.base = manifest.get("snapshot", {}).get("merge_base", "")
        self._files, self._diffs = None, {}

    def run(self, args):
        if not all(re.fullmatch(r"[a-f0-9]{40,64}", sha) for sha in (self.head, self.base)):
            raise ApiError("context_unavailable", "This saved review has no complete Git snapshot.")
        return git(["--git-dir", self.common, *args]).stdout

    def files(self):
        if self._files is None:
            fields = self.run(["diff", "--name-status", "-z", "--find-renames", self.base, self.head, "--"]).split("\0")
            self._files = []
            while fields and fields[0]:
                status, old = fields.pop(0), fields.pop(0)
                new = fields.pop(0) if status[0] in "RC" else old
                self._files.append({"path": new, "old_path": old, "status": status})
        return self._files

    def diff(self, path):
        safe_path(path)
        if path not in self._diffs:
            entry = next((f for f in self.files() if f["path"] == path), None)
            if entry is None:
                raise ApiError("invalid_anchor", "File is not in this review's diff.")
            patch = self.run(["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=3",
                              self.base, self.head, "--", entry["old_path"], path])
            self._diffs[path] = {**entry, "patch": patch, "hunks": parse_hunks(patch)}
        return self._diffs[path]

    def anchor(self, location):
        if not isinstance(location, dict):
            raise ApiError("invalid_anchor", "Choose a code line or range before staging.")
        path, side = location.get("path"), location.get("side", "RIGHT")
        end = location.get("line", location.get("end_line"))
        start = location.get("start_line", end)
        if side not in ("LEFT", "RIGHT") or type(end) is not int or type(start) is not int or not 0 < start <= end:
            raise ApiError("invalid_anchor", "Choose positive lines on one diff side.")
        # LEFT means deletions; context and additions attach to RIGHT, as GitHub documents.
        key = "old" if side == "LEFT" else "new"
        for hunk in self.diff(path)["hunks"]:
            lines = {row[key] for row in hunk["rows"]
                     if row[key] is not None and (side == "RIGHT" or row["kind"] == "-")}
            if end - start < len(lines) and all(n in lines for n in range(start, end + 1)):
                return {"kind": "inline", "path": path, "side": side, "line": end,
                        "start_line": start, "commit": self.head}
        raise ApiError("invalid_anchor", "The complete range must be on a publishable side of one diff hunk.")

    def context(self, anchor):
        value = self.anchor(anchor)
        key = "old" if value["side"] == "LEFT" else "new"
        entry = self.diff(value["path"])
        hunk = next(h for h in entry["hunks"] if any(r[key] == value["line"] for r in h["rows"]))
        return {**value, "commit": self.base if value["side"] == "LEFT" else self.head,
                "old_path": entry["old_path"], "header": hunk["header"],
                "rows": [{**r, "highlight": r[key] is not None and value["start_line"] <= r[key] <= value["line"]}
                         for r in hunk["rows"]]}

    def source(self, path, side="RIGHT", commit=None):
        safe_path(path)
        if side not in ("LEFT", "RIGHT"):
            raise ApiError("invalid_anchor", "Unknown diff side.")
        revision = commit or (self.base if side == "LEFT" else self.head)
        if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
            raise ApiError("invalid_anchor", "Invalid saved revision.")
        if side == "LEFT":
            path = next((f["old_path"] for f in self.files() if f["path"] == path), path)
        return {"path": path, "commit": revision, "text": self.run(["show", f"{revision}:{path}"])}

    def pin(self, generation):
        for suffix, sha in (("head", self.head), ("merge-base", self.base)):
            self.run(["update-ref", f"refs/pr-review/api/{generation}/{suffix}", sha])


def parse_hunks(patch):
    hunks, active = [], None
    old = new = old_left = new_left = 0
    for line in patch.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            old, new = int(match[1]), int(match[3])
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
            active = {"header": line, "rows": []}
            hunks.append(active)
        elif active is not None and line[:1] in (" ", "+", "-") and (old_left or new_left):
            kind = line[0]
            active["rows"].append({"kind": kind, "text": line[1:],
                                   "old": old if kind != "+" else None,
                                   "new": new if kind != "-" else None})
            if kind != "+":
                old += 1
                old_left -= 1
            if kind != "-":
                new += 1
                new_left -= 1
        elif line.startswith("diff --git "):
            active = None
    return hunks
