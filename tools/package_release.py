"""Build a runtime-only ZIP from an allowlist, never from the whole checkout."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

PACKAGE_NAME = "ComfyUI-Universal-Skills-Director"
ROOT_FILES = (
    "__init__.py",
    "nodes.py",
    "director_nodes.py",
    "README.md",
    "LICENSE",
    ".gitignore",
    "requirements.txt",
    "ush_config.example.json",
    "docs/ARCHITECTURE.md",
    "docs/PROJECT_SPEC.md",
    "web/js/specification_drop.js",
)
_CREDENTIAL = re.compile(rb"sk-[A-Za-z0-9_-]{16,}")


def release_contents(root):
    root = Path(root).resolve(strict=True)
    candidates = [root / name for name in ROOT_FILES]
    candidates.extend(sorted((root / "universal_skills").rglob("*.py")))
    contents = {}
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_nlink != 1
            or not path.resolve().is_relative_to(root)
            or any(parent.is_symlink() for parent in path.parents if parent != root)
        ):
            raise ValueError("Missing or unsafe release source.")
        data = path.read_bytes()
        if _CREDENTIAL.search(data):
            raise ValueError("Possible credential in release source; packaging refused.")
        contents[f"{PACKAGE_NAME}/{relative}"] = data
    template = json.loads(contents[f"{PACKAGE_NAME}/ush_config.example.json"])
    if template.get("openai_api_key") != "":
        raise ValueError("Release config template must have an empty API key.")
    return contents


def build_release(root, output):
    root = Path(root).resolve(strict=True)
    output = Path(output).absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError("Place the release ZIP outside the source directory.")
    contents = release_contents(root)
    # Exclusive creation: never overwrite an earlier release or user file.
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            archive.writestr(name, data)
    with ZipFile(output) as archive:
        if set(archive.namelist()) != set(contents) or archive.testzip() is not None:
            raise ValueError("Release archive verification failed.")
        for name, data in contents.items():
            if archive.read(name) != data:
                raise ValueError("Release/source content mismatch.")
    return {
        "files": len(contents),
        "bytes": output.stat().st_size,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New ZIP outside the source directory")
    args = parser.parse_args()
    result = build_release(Path(__file__).resolve().parents[1], args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
