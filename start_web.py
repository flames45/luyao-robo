"""Browser/server entry point; public demo mode is the default."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn


def main() -> None:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    os.environ.setdefault("LINGSHU_PUBLIC_MODE", "1")
    os.environ.setdefault("LINGSHU_DATA_DIR", str(root / "web_data"))
    parser = argparse.ArgumentParser(description="启动路遥网页版")
    parser.add_argument("--host", default=os.getenv("LINGSHU_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT") or os.getenv("LINGSHU_PORT", "8002")))
    args = parser.parse_args()
    os.environ["LINGSHU_PORT"] = str(args.port)
    print(f"路遥网页版：http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}", flush=True)
    print("公开演示模式" if os.environ["LINGSHU_PUBLIC_MODE"] == "1" else "本机管理模式", flush=True)
    uvicorn.run("api_server:app", host=args.host, port=args.port, workers=1, proxy_headers=True)


if __name__ == "__main__":
    main()
