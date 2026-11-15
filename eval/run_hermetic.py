"""Start ONLY the fake LLM and the sealed agent (fake internet), run the hermetic eval subset, tear everything down.

    research-agent/.venv/Scripts/python.exe eval/run_hermetic.py

Needs the isolated test Redis from AGENTS.md (127.0.0.1:56379). Nothing leaves the machine; it validates the harness and the
agent's SSE protocol with a deterministic fake model, so it says nothing about real answer quality.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
P_LLM, P_AGENT = 18991, 18041
KEY = "eval-agent-key-" + "q9" * 12
ENV = {
    **os.environ, "PYTHONUNBUFFERED": "1", "REDIS_URL": "redis://127.0.0.1:56379/6", "AGENT_API_KEY": KEY,
    "OPENROUTER_API_KEY": "fake-key", "OPENROUTER_BASE_URL": f"http://127.0.0.1:{P_LLM}/v1", "OPENAI_API_KEY": "fake-openai-key", "LOG_LEVEL": "WARNING",
    "E2E_OUT": str(ROOT / "eval" / "results"),
}


def wait(url, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    (ROOT / "eval" / "results").mkdir(parents=True, exist_ok=True)
    procs = []
    try:
        procs.append(subprocess.Popen([PY, str(ROOT / "e2e" / "fake_llm.py"), str(P_LLM)], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([PY, str(ROOT / "e2e" / "run_agent_fake_internet.py"), str(P_AGENT)], env=ENV, cwd=ROOT / "research-agent",
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        if not (wait(f"http://127.0.0.1:{P_LLM}/health", 30) and wait(f"http://127.0.0.1:{P_AGENT}/health", 240)):
            print("fake stack failed to start")
            return 3
        r = subprocess.run([PY, str(ROOT / "eval" / "run_eval.py"), "--url", f"http://127.0.0.1:{P_AGENT}", "--hermetic"], env=ENV)
        leaked = (ROOT / "eval" / "results" / "blocked-requests.log")
        leaked_text = leaked.read_text().strip() if leaked.exists() else ""
        print("HERMETIC CHECK (agent sealed at socket level):", "PASS - nothing left the machine" if not leaked_text else "FAIL\n" + leaked_text)
        return r.returncode or (1 if leaked_text else 0)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
