#!/usr/bin/env python3
"""CoverageFlow launcher — one command to run the whole application.

    python3 launch.py                # first run: onboarding wizard, then start
    python3 launch.py --configure    # redo the onboarding wizard
    python3 launch.py --port 8765    # pick another port
    python3 launch.py --no-browser   # start without opening a browser
    python3 launch.py --yes          # accept all defaults (non-interactive)

What it does, in order:
  1. makes sure a python virtualenv exists (.venv) with the dependencies;
  2. first run only: asks which backend role dispatch should use
     (offline demo / OpenAI-compatible API / headless CLI) and where the
     workspace lives, and saves the answers;
  3. prepares the workspace (creates it and seeds a ready-to-run Intel demo
     case when it is empty);
  4. starts the web service (UI + API in one process) and opens it;
  5. Ctrl+C stops everything cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCH_CFG = ROOT / ".coverageflow-launch.json"   # launcher answers (no secrets)
API_CFG = ROOT / ".coverageflow.json"              # {"api": {...}} (may hold a key)
DEFAULT_PORT = 8765
DEMO_MANIFEST = ROOT / "examples" / "run1-clean" / "inputs-manifest.json"
DEMO_CASE_ID = "intc-fy2026q3-20261030"
IS_WINDOWS = os.name == "nt"


# ----------------------------------------------------------------- io utils
def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str, default: str = "", yes: bool = False) -> str:
    if yes:
        return default
    suffix = f" [{default}]" if default != "" else " []"
    try:
        return input(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        return default


def ask_choice(prompt: str, options: list[str], default: str,
               yes: bool = False) -> str:
    """Pick from a fixed set; anything else re-prompts (or default in yes)."""
    if yes:
        return default
    while True:
        try:
            raw = input(f"{prompt} ({'/'.join(options)}) [{default}]: ").strip()
        except EOFError:
            return default
        if not raw:
            return default
        if raw in options:
            return raw
        say(f"  please type one of: {', '.join(options)}")


def ask_secret(prompt: str, yes: bool = False) -> str:
    if yes:
        return ""
    try:
        import getpass
        return getpass.getpass(f"{prompt}: ").strip()
    except EOFError:
        return ""


# ------------------------------------------------------------ environment
def venv_python() -> Path:
    sub = "Scripts" if IS_WINDOWS else "bin"
    return ROOT / ".venv" / sub / ("python.exe" if IS_WINDOWS else "python")


def ensure_env() -> Path:
    """Create .venv + install dependencies when missing (first-run setup)."""
    py = venv_python()
    if py.is_file():
        return py
    say("· creating the virtual environment (.venv) — first run only ...")
    venv_mod = f"Scripts\\python.exe" if IS_WINDOWS else "bin/python"
    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")],
                   check=True, cwd=str(ROOT))
    py = ROOT / ".venv" / venv_mod
    subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                    "--upgrade", "pip"], check=True)
    say("· installing dependencies (requirements.txt) ...")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                    "-r", str(ROOT / "requirements.txt")], check=True,
                   cwd=str(ROOT))
    return py


# ------------------------------------------------------------- onboarding
def detect_clis(py: Path) -> list[str]:
    """Which headless CLI binaries respond to --help on this machine."""
    found = []
    for name in ("claude", "codex", "kimi", "vibe"):
        try:
            r = subprocess.run([name, "--version"], capture_output=True,
                               text=True, timeout=25)
            if r.returncode == 0:
                found.append(name)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return found


def onboard(py: Path, yes: bool) -> dict:
    say("=" * 68)
    say("CoverageFlow first-run setup")
    say("=" * 68)
    say("""
This is a long-running equity-research workflow. Role dispatches (the
generator/critic model calls) can run three ways:

  1. offline demo — canned, gate-satisfying role responses. No account,
     no API key, no network. Everything else (engines, gates, critics'
     numerical checks, budget, approval) runs for real. Best to explore.
  2. api          — any OpenAI-compatible chat API (Mistral, OpenAI, ...),
     needs base URL + model + API key.
  3. cli          — a headless coding CLI already logged in on this
     machine (claude / codex / kimi / vibe — the Mistral CLI).""")
    mode = ask_choice("Mode", ["1", "2", "3"], "1", yes=yes)
    cfg: dict = {"version": 1, "mode": {"1": "offline", "2": "api",
                                        "3": "cli"}[mode]}

    ws = ask("Workspace directory (cases live here)", "workspace", yes=yes)
    cfg["workspace"] = str(Path(ws))
    cfg["seed_demo_case"] = True

    if cfg["mode"] == "api":
        say("\n· API configuration (OpenAI-compatible /chat/completions)")
        base = ask("  API base URL", "https://api.mistral.ai/v1", yes=yes)
        model = ask("  Model name (e.g. mistral-medium-3.5, "
                    "mistral-large-latest)", "mistral-medium-3.5", yes=yes)
        key = ask_secret("  API key (hidden; stored in .coverageflow.json "
                         "— keep it gitignored)", yes=yes)
        if yes and not key:
            say("  (defaults mode: no key set — role runs will fail until "
                "you add one to .coverageflow.json)")
        if base:
            API_CFG.write_text(json.dumps(
                {"api": {"base_url": base, "model": model,
                         **({"api_key": key} if key else {})}}, indent=2))
            say(f"  written: {API_CFG.name}")
        cfg["backend"] = "api"

    elif cfg["mode"] == "cli":
        available = detect_clis(py)
        if not available:
            say("\n! no headless CLIs found on PATH "
                "(claude/codex/kimi/vibe) — falling back to the offline demo.")
            cfg["mode"] = "offline"
            cfg["backend"] = "stub"
        else:
            say(f"\n· detected CLIs: {', '.join(available)}")
            pick = ask_choice("  Which one", available, available[0], yes=yes)
            cfg["backend"] = pick
    else:
        cfg["backend"] = "stub"

    port = ask(f"Web UI port", str(DEFAULT_PORT), yes=yes)
    cfg["port"] = max(1024, min(65535, int(port) if port.isdigit()
                                else DEFAULT_PORT))

    LAUNCH_CFG.write_text(json.dumps(cfg, indent=2))
    say(f"\n· configuration saved to {LAUNCH_CFG.name}")
    say("  (delete it or run with --configure to set up again)")
    return cfg


def load_or_onboard(py: Path, yes: bool, reconfigure: bool) -> dict:
    if LAUNCH_CFG.is_file() and not reconfigure:
        cfg = json.loads(LAUNCH_CFG.read_text())
        if not reconfigure:
            say(f"· using saved configuration ({LAUNCH_CFG.name}) "
                f"— mode: {cfg.get('mode')}")
        return cfg
    return onboard(py, yes)


# -------------------------------------------------------------- workspace
def prepare_workspace(py: Path, cfg: dict) -> tuple[Path, str | None]:
    """Create the workspace; seed a ready-to-run demo case when empty."""
    ws = (ROOT / cfg["workspace"]).resolve() if cfg["workspace"] != "." else ROOT
    (ws / "cases").mkdir(parents=True, exist_ok=True)

    def cases() -> list[Path]:
        return [d for d in (ws / "cases").iterdir()
                if d.is_dir() and (d / "state.json").is_file()]

    live = [d for d in cases()
            if json.loads((d / "state.json").read_text()).get("status")
            not in ("CLOSED", "APPROVED", "REJECTED")]
    if live:
        return ws, None
    if cases() and not cfg.get("seed_demo_case"):
        return ws, None
    if (ws / "cases" / DEMO_CASE_ID / "state.json").is_file() or cases():
        if not cfg.get("seed_demo_case"):
            return ws, None
        # workspace only holds terminal cases — seed a fresh runnable one
        casedir = ws / "cases" / DEMO_CASE_ID
        if casedir.exists():
            return ws, None

    if not DEMO_MANIFEST.is_file():
        say("! demo bundle manifest missing — starting with an empty "
            "workspace (create a case from the UI wizard)")
        return ws, None
    say(f"· seeding the ready-to-run Intel demo case ({DEMO_CASE_ID}) "
        f"into {cfg['workspace']}/")
    r = subprocess.run([str(py), "-m", "coverageflow", "--workspace", str(ws),
                        "case", "new", "--ticker", "INTC", "--period",
                        "fy2026q3", "--trigger-doc", "intc-10q-2026q3",
                        "--cutoff", "2026-10-30", "--manifest",
                        str(DEMO_MANIFEST)], capture_output=True, text=True,
                       cwd=str(ROOT))
    if r.returncode != 0:
        say(f"! seeding failed (the UI wizard can still create cases):\n"
            f"{r.stdout}\n{r.stderr}")
        return ws, None
    return ws, DEMO_CASE_ID


# ------------------------------------------------------------------ server
def free_port(start: int) -> int:
    import socket
    for port in range(start, start + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def wait_healthy(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def select_backend(port: int, name: str) -> None:
    body = json.dumps({"backend": name}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/backends/select", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        say(f"· role dispatch backend selected: {name}")
    except (urllib.error.URLError, OSError) as exc:
        say(f"! could not select backend '{name}' "
            f"(pick one on the UI Backends page): {exc}")


def open_browser(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        say(f"  open {url} in your browser")


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(prog="launch.py", description=__doc__)
    ap.add_argument("--configure", action="store_true",
                    help="redo the first-run wizard even if configured")
    ap.add_argument("--port", type=int, default=None,
                    help="web UI port (overrides the saved configuration)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser tab")
    ap.add_argument("--yes", action="store_true",
                    help="accept all defaults (non-interactive)")
    args = ap.parse_args()

    say("CoverageFlow launcher")
    py = ensure_env()
    cfg = load_or_onboard(py, args.yes, args.configure)
    if args.port:
        cfg["port"] = args.port

    ws, seeded = prepare_workspace(py, cfg)
    port = free_port(int(cfg.get("port", DEFAULT_PORT)))

    say(f"· starting the web service on http://127.0.0.1:{port} ...")
    env = dict(os.environ)
    env["COVERAGEFLOW_WORKSPACE"] = str(ws)
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "ui.api:app", "--host", "127.0.0.1",
         "--port", str(port)], cwd=str(ROOT), env=env)

    if not wait_healthy(port):
        proc.terminate()
        say("! the service did not become healthy in time — "
            "see the output above")
        return 1

    backend = cfg.get("backend", "auto")
    if backend not in ("", "auto"):
        select_backend(port, backend)

    url = f"http://127.0.0.1:{port}"
    say("")
    say("=" * 68)
    say(f"  CoverageFlow is running:  {url}")
    if seeded:
        say(f"  Demo case ready to run: {seeded}")
        if backend == "stub":
            say("  1) open the case → Build tab → ▶ Run (offline demo)")
            say("  2) approve it on the Approval tab → export the dossier")
    say("  stop with Ctrl+C in this terminal")
    say("=" * 68)
    say("")

    if not args.no_browser:
        open_browser(url)

    try:
        proc.wait()
    except KeyboardInterrupt:
        say("\n· stopping the service ...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        say("· stopped. bye.")
    return proc.returncode if proc.returncode in (0, None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
