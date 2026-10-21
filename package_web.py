"""Build a source-only web submission, using an explicit file allowlist."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = [
    "api_server.py", "vita_crew.py", "skills.py", "robo_actions.py", "robot_bridge.py",
    "start_web.py", "index.html", "run_web.bat", "requirements.web.txt", "requirements.test.txt",
    ".env.example", ".gitignore", ".dockerignore", "Dockerfile", "compose.yaml", "Caddyfile",
    "render.yaml", "网页部署说明.md", "网页修复记录.md", "package_web.py",
]


def main():
    output = ROOT / "submission" / "Robo_Web.zip"
    output.parent.mkdir(exist_ok=True)
    paths = [ROOT / name for name in FILES]
    for folder in ["assets", "live2d", "tests"]:
        paths.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
            archive.write(path, "Robo_Web/" + path.relative_to(ROOT).as_posix())
    print(output)
    print(f"{output.stat().st_size / 1024 / 1024:.2f} MB; {len(paths)} files; excludes secrets and user data")


if __name__ == "__main__":
    main()
