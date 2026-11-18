"""Export an allowlisted public ZIP with portable paths and verify extraction."""
import argparse
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# Explicit source allowlist: original documents, databases, caches and other archives never enter the package.
FILES = (
    ".gitignore", ".dockerignore", ".env.example", ".vscode/tasks.json",
    "Dockerfile", "compose.yaml", "Caddyfile", "railway.toml", "render.yaml",
    "README.md", "server.py", "demo.py", "workflow.py", "policies.py",
    "public/index.html", "public/app.js", "public/style.css",
    "docs/ARCHITECTURE.md", "docs/DELIVERY.md",
    "docs/screenshots/welcome.png", "docs/screenshots/overview.png", "docs/screenshots/manager-approval.png",
    "docs/screenshots/finance-reconciliation.png", "docs/screenshots/admin-policies.png",
    "docs/screenshots/mobile-overview.png",
    "tests/test_workflow.py", "tests/test_policies.py", "tests/test_package.py", "tests/test_demo.py",
    "scripts/package_public.py",
)


def verify(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP checksum verification failed.")
        for name in archive.namelist():
            relative = PurePosixPath(name)
            if "\\" in name or relative.is_absolute() or ".." in relative.parts or ":" in name:
                raise ValueError(f"Non-portable archive path: {name}")
        if set(archive.namelist()) != set(FILES) | {"public/", "docs/", "tests/", "scripts/", ".vscode/"}:
            raise ValueError("Archive differs from the public file allowlist.")
        with tempfile.TemporaryDirectory(prefix="travel-desk-extract-") as directory:
            archive.extractall(directory)
            for relative in FILES:
                if not (Path(directory) / relative).is_file():
                    raise ValueError(f"File did not extract: {relative}")
            for relative in ("public", "docs", "tests"):
                if not (Path(directory) / relative).is_dir():
                    raise ValueError(f"Directory did not extract: {relative}")


def build(root=ROOT, output=None):
    root = Path(root)
    output = Path(output) if output else root / "Travel_Desk_Public_v3.zip"
    for relative in FILES:
        source = root / relative
        try:
            source.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"Source is outside the workspace: {relative}") from None
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing or unsafe public source: {relative}")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in ("public/", "docs/", "tests/", "scripts/", ".vscode/"):
            archive.writestr(directory, "")
        for relative in FILES:
            # as_posix is explicit even when packaging on Windows.
            archive.write(root / relative, arcname=PurePosixPath(relative).as_posix())
    verify(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    result = build(output=options.output)
    print(f"Created and verified {result.name}: {result.stat().st_size} bytes")
