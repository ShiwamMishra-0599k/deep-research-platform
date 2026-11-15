"""The API documentation cannot silently drift from the application."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS = ROOT / "docs"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_openapi", ROOT / "scripts" / "gen_openapi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_openapi_json_matches_the_application():
    built = _generator().build()
    committed = (DOCS / "openapi.json").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == built, "docs/openapi.json is stale: run `python scripts/gen_openapi.py` and commit the result"


def test_every_endpoint_is_documented_in_api_md():
    import json

    schema = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("|")]
    missing = []
    for path, ops in schema["paths"].items():
        norm = re.sub(r"\{[^}]+\}", "{id}", path)
        for method in ops:
            if not any(f"| {method.upper()} |" in r and f"`{norm}`" in r for r in rows):
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"add these endpoints to docs/API.md: {missing}"


def test_api_md_does_not_document_endpoints_that_no_longer_exist():
    import json

    schema = json.loads((DOCS / "openapi.json").read_text(encoding="utf-8"))
    real = {(m.upper(), re.sub(r"\{[^}]+\}", "{id}", p)) for p, ops in schema["paths"].items() for m in ops}
    text = (DOCS / "API.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| (GET|POST|PATCH|DELETE|PUT) \| `(/[^`]*)`", text, flags=re.M))
    assert documented <= real, f"documented but not implemented: {documented - real}"
