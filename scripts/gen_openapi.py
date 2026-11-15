"""Write docs/openapi.json from the backend's own schema, or check that the committed copy is current.

    backend/.venv/Scripts/python.exe scripts/gen_openapi.py           # (re)write docs/openapi.json
    backend/.venv/Scripts/python.exe scripts/gen_openapi.py --check   # exit 1 if the committed file is stale

The running service deliberately exposes no /docs or /openapi.json (it would publish the whole API surface), so the schema is produced
offline from the application object. It needs no database or Redis: only settings validation runs at import, using throwaway values below.
The backend test suite also fails when docs/openapi.json is stale (backend/tests/test_api_docs.py).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "openapi.json"

# Throwaway values so `Settings()` validates; nothing connects and nothing here is a real secret.
os.environ.setdefault("JWT_SECRET", "doc-gen-" + "a1b2c3d4" * 6)
os.environ.setdefault("CSRF_SECRET", "doc-gen-" + "e5f6a7b8" * 6)
os.environ.setdefault("AGENT_API_KEY", "doc-gen-agent-key-0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://doc:doc@127.0.0.1:5432/doc")
os.environ.setdefault("ALLOW_INSECURE_DEV_DB", "1")
sys.path.insert(0, str(ROOT / "backend"))


def build() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8").replace("\r\n", "\n") != text:
            print("docs/openapi.json is stale: run scripts/gen_openapi.py and commit the result")
            return 1
        print("docs/openapi.json is current")
        return 0
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
