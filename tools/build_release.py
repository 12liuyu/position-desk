"""Build an allowlisted source ZIP. Never publish or call Git."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore", "README.md", "LICENSE", "requirements.txt", "start_demo.cmd",
    "app.py", "engine.py", "planner.py", "sources.py", "examples/demo.json",
    "docs/data-format.md", "docs/demo.jpg", "tests/test_core.py", "tests/test_ui.py",
    "tests/test_release.py", "tools/build_release.py",
    "SKILL.md", "agents/openai.yaml", "scripts/inspect_positions.py",
    "docs/quick-demo.md", "tests/test_skill.py",
)
PATTERNS = {
    "personal Windows path": r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_. -]+[\\/]",
    "personal home path": r"/(?:home|Users)/[A-Za-z0-9_.-]+/",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "access credential": r"\b(?:ghp_|github_pat_|sk-proj-|sk-)[A-Za-z0-9_-]{20,}",
    "literal secret": r'''(?i)(?:api_key|access_token|password)\s*[=:]\s*["'][A-Za-z0-9_/-]{16,}["']''',
}


def linked(path):
    if path.is_symlink():
        return True
    try:
        # Windows reparse points include junctions; works with Python 3.11 too.
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def inspected_files(root):
    root = Path(root).resolve()
    contents = {}
    for name in FILES:
        path = root / name
        if any(linked(p) for p in [path, *path.parents] if p != root.parent):
            raise ValueError(f"Link or junction is not allowed: {name}")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f"Required release file missing or outside root: {name}")
        data = path.read_bytes()
        if len(data) > 2_000_000:
            raise ValueError(f"Oversized release file: {name}")
        if name.endswith(".jpg"):
            if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
                raise ValueError("Expected a manually reviewed demo JPEG")
        else:
            text = data.decode("utf-8-sig")
            for label, pattern in PATTERNS.items():
                if re.search(pattern, text):
                    raise ValueError(f"Potential {label} in {name}; inspect locally")
        contents[name] = data
    return contents


def build(root=ROOT):
    root = Path(root).resolve()
    contents = inspected_files(root)
    output = root / "dist"
    if linked(output):
        raise ValueError("Output directory cannot be a link")
    output.mkdir(exist_ok=True)
    target = output / "position-desk-source.zip"
    fd, temporary = tempfile.mkstemp(dir=output, suffix=".tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in contents.items():
                info = zipfile.ZipInfo("position-desk/" + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, data)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    manifest = {"status": "local source archive; building does not publish", "license": "MIT", "file_count": len(contents),
        "archive_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    result = build()
    print(json.dumps({"file_count": result["file_count"], "sha256": result["archive_sha256"]}))
