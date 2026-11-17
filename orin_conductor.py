#!/usr/bin/env python3
"""
OrinConductor v4: Multi-Game Porting & Triage Studio + Carbon Autopilot Agent
for OrinNative on Jetson AGX Orin 32GB.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent
RAW_DIR = Path.home() / "orin-switch2" / "raw_backups"
PACKS_DIR = WORKDIR / "packs"
DB_PATH = WORKDIR / "conductor_state.db"
MISSION_FILE = WORKDIR / "carbon_mission.txt"
PORT = int(os.environ.get("CONDUCTOR_PORT", "7860"))

KNOWN_GAMES = {
    "0400000000010000": "Workload-A 3D Engine (Canary — 60 FPS)",
    "0400000000020000": "Workload-B 3D Engine",
    "0400000000030000": "Workload-C Voxel Engine",
    "0400000000040000": "Workload-D 3D Engine",
    "0400000000050000": "Workload-E Flight Engine",
}

DEFAULT_Workload-B_MISSION = "\n".join([
    "MISSION: PREP, BUILD, PACK & BOOT WORKLOAD-B 3D ENGINE (0400000000020000 v1.8.0 / 917504)",
    "1. INSPECT Workload-B PROFILE & DUMP (MAX 3 TURNS):",
    "   - Read `dm/games/0400000000020000/game.tsv` and `dm/games/0400000000020000/README.md`.",
    "   - Check `ls -la ~/orin-switch2/raw_backups/0400000000020000/updates/917504/exefs`.",
    "   - `launch_game.sh` already resolves standalone `updates/917504/{exefs,romfs}`.",
    "2. BUILD HOST BINARY & PACK Workload-B (`./launch_game.sh prep 0400000000020000`):",
    "   - First run `./launch_game.sh build 0400000000020000` to compile `dm/host/build/orin_host_0400000000020000`.",
    "     Fix any game-specific compile/link errors if they occur.",
    "   - Next run `./launch_game.sh prep 0400000000020000` to run `scripts/pack_android.py` into",
    "     `packs/0400000000020000` and `packs/0400000000020000-shaders`.",
    "   - If `pack_android.py` or the .NET rewriter reports symbol/offset mismatches with update `917504` vs",
    "     `dm/games/0400000000020000/game.tsv`, inspect and update `game.tsv` / hooks accordingly.",
    "3. BOOT Workload-B & TRIAGE RUNTIME ISSUES:",
    "   - Launch Workload-B via `./launch_game.sh play 0400000000020000`.",
    "   - Inspect `play_0400000000020000.log` and capture screenshots via `./launch_game.sh shot` (`boot_screen.png`).",
    "   - If Workload-B crashes, aborts, or hangs on an unimplemented HLE service, guest hook, or Vulkan path,",
    "     diagnose from `play_0400000000020000.log` (or gdb if needed), patch cleanly, recompile with",
    "     `./launch_game.sh build 0400000000020000`, and re-test.",
    "4. CRITICAL PRESERVATION RULES:",
    "   - NEVER break Workload-A (`0400000000010000`) or overwrite our Linux/Orin fixes in `44a2998e` & `b2e857dd`",
    "     (`plat_linux.c`, `front_linux.c`, `movie_linux.c`, target 10 texel buffers, `vka_ixc_spv.h` indirect quads,",
    "     `vka_draw.c` colorWriteMask).",
    "   - Commit working changes cleanly to git.",
])

PRESET_MISSIONS = {
    "mkw_hang": "MISSION: FIX WORKLOAD-B 3D ENGINE (0400000000020000) GPU HANG & ADVANCE INTO GAMEPLAY\n1. CURRENT STATE:\n   - Workload-B boots and renders the 3D title screen (`boot_screen.png`, brightness ~0.62, 743 pipelines cached in `/home/Chase/vk_pipelines-fp0.bin`), but then logs `[dm] received GPU hang: graceful exit` in `play_0400000000020000.log` and exits!\n2. DIAGNOSE & FIX THE GPU HANG:\n   - Inspect `play_0400000000020000.log` and check where `received GPU hang` is triggered in `dm/gfx/vk_android/` (`vka_cmd.c`, `vka_draw.c`, `vka_comp.c`, etc.).\n   - Determine whether a specific compute/graphics shader, indirect dispatch/draw, or fence timeout is causing `VK_ERROR_DEVICE_LOST` / queue hang on the title screen.\n   - Patch `dm/gfx/vk_android/` cleanly, recompile with `./launch_game.sh build 0400000000020000`, and re-launch `./launch_game.sh play 0400000000020000`.\n3. ADVANCE PAST 'PRESS L + R TO START' INTO GAMEPLAY:\n   - Once Workload-B runs for 30+ seconds without `received GPU hang`, check `dm/host/front_linux.c` for the keyboard mapping of `L`, `R`, and `A`.\n   - Use `DISPLAY=:0 xdotool` to press `L + R` and `A` to advance past the title screen into the menu / race, capture `boot_screen.png` via `./launch_game.sh shot`, and verify FPS & stability.\n4. GUARDRAILS:\n   - Do NOT break Workload-A (`0400000000010000`). Commit working fixes cleanly to git.",
    "mkw_boot": DEFAULT_Workload-B_MISSION,
    "triage_selected": "\n".join([
        "MISSION: PREP, BUILD, BOOT & TRIAGE SELECTED GAME",
        "1. Read `dm/games/<GAME_ID>/game.tsv` and `README.md` for the currently selected game.",
        "2. Run `./launch_game.sh prep <GAME_ID>` to compile `dm/host/build/orin_host_<GAME_ID>` and pack NSOs + shaders.",
        "3. Run `./launch_game.sh play <GAME_ID>` and inspect `play_<GAME_ID>.log` + `./launch_game.sh shot` (`boot_screen.png`).",
        "4. Diagnose any crash, missing HLE service, stub, or Vulkan rendering error; patch cleanly; recompile with `./launch_game.sh build <GAME_ID>`; verify; and commit to git.",
        "5. Preserve Workload-A (`0400000000010000`) and all Linux/Orin fixes (`44a2998e`, `b2e857dd`).",
    ]),
    "upstream_sync": "\n".join([
        "MISSION: SAFE UPSTREAM SYNC FROM BANANANATIVE & NXNATIVE",
        "1. Run `git fetch origin android && git fetch upstream-upstream-core main`.",
        "2. Inspect `git log HEAD..origin/android --oneline` and `git log HEAD..upstream-upstream-core/main --oneline -- dm/host/ dm/tools/ dm/games/`.",
        "3. Selectively port ONLY high-value improvements (new `dm/games/` profiles, `dm/host/hle_*.c` fixes, `dm/tools/`, and safe `dm/gfx/vk_android/` fixes).",
        "4. NEVER overwrite `plat_linux.c`, `front_linux.c`, `movie_linux.c`, `dm/host/Makefile`, `launch_game.sh`, or our fixes from `44a2998e` & `b2e857dd`.",
        "5. Verify `./launch_game.sh build 0400000000010000` and `./launch_game.sh build 0400000000020000`, and commit cleanly.",
    ]),
    "audit_codebase": "\n".join([
        "MISSION: AUDIT & CLEAN UP AI-PATCHED HOST / VULKAN FILES",
        "1. Inspect `git diff upstream-upstream-core/main...HEAD --stat` and audit our Linux/Vulkan files (`dm/host/plat/plat_linux.c`, `dm/host/front_linux.c`, `dm/host/movie_linux.c`, `dm/gfx/vk_android/*.c`).",
        "2. Check for dead code, duplicate blocks, compiler warnings, or subtle bugs introduced by piecemeal patches.",
        "3. Clean up any issues without altering verified behavior (`44a2998e` & `b2e857dd`).",
        "4. Verify builds for `0400000000010000` and `0400000000020000` and smoke-test before committing.",
    ]),
}

CARBON_TOOLS = [{
    "functionDeclarations": [
        {
            "name": "run_bash",
            "description": "Execute a bash command inside ~/orin-switch2/orin-native on the Jetson AGX Orin and return exit code + output.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"command": {"type": "STRING", "description": "Shell command to run"}},
                "required": ["command"],
            },
        },
        {
            "name": "read_file",
            "description": "Read lines from a file in the repository (1-indexed).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING"},
                    "start_line": {"type": "INTEGER"},
                    "end_line": {"type": "INTEGER"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Create or overwrite a file in the repository with complete content.",
            "parameters": {
                "type": "OBJECT",
                "properties": {"path": {"type": "STRING"}, "content": {"type": "STRING"}},
                "required": ["path", "content"],
            },
        },
        {
            "name": "patch_file",
            "description": "Replace a search snippet with a replace snippet inside a file.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING"},
                    "search": {"type": "STRING"},
                    "replace": {"type": "STRING"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    ]
}]


class ConductorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop_requested = False
        self.current_stage = 6
        self.game_id = "0400000000020000"
        self.carbon_calls = 0
        self.spend_usd = 0.0
        self.budget_usd = 3.00
        self.active_model = "Idle"
        self.preferred_model = "auto"
        self.last_brightness = "N/A"
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.vacation_mode = False
        self.vac_start = "01:30"
        self.vac_end = "08:30"
        self.eco_sleeping = False
        self.manual_awake = False
        self.last_wake_date = ""
        self.mission_prompt = DEFAULT_Workload-B_MISSION
        self.logs = []
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, stage INT, "
                "strike INT, model TEXT, summary TEXT, commit_hash TEXT)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            for k, attr, cast in [
                ("game_id", "game_id", str),
                ("carbon_calls", "carbon_calls", int),
                ("spend_usd", "spend_usd", float),
                ("api_key", "api_key", str),
                ("vacation_mode", "vacation_mode", lambda x: x == "True"),
                ("vac_start", "vac_start", str),
                ("vac_end", "vac_end", str),
                ("mission_prompt", "mission_prompt", str),
                ("preferred_model", "preferred_model", str),
            ]:
                row = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
                if row and row[0]:
                    try:
                        setattr(self, attr, cast(row[0]))
                    except Exception:
                        pass
        if MISSION_FILE.exists():
            txt = MISSION_FILE.read_text(errors="replace").strip()
            if txt:
                self.mission_prompt = txt
        else:
            MISSION_FILE.write_text(self.mission_prompt + "\n")

    def save_kv(self, k, v):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, str(v)))

    def set_mission(self, prompt_text):
        prompt_text = prompt_text.strip()
        if not prompt_text:
            return
        self.mission_prompt = prompt_text
        self.save_kv("mission_prompt", prompt_text)
        MISSION_FILE.write_text(prompt_text + "\n")

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 250:
                self.logs.pop(0)

    def record_event(self, stage, strike, model, summary, commit_hash=""):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO history (ts, stage, strike, model, summary, commit_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, stage, strike, model, summary, commit_hash),
            )


STATE = ConductorState()


def run_cmd(cmd, timeout=600):
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    if "XAUTHORITY" not in env and subprocess.run("pgrep -x Xwayland >/dev/null 2>&1", shell=True).returncode == 0:
        for auth in list(Path("/run/user").glob("*/.mutter-Xwaylandauth.*")) + list(Path("/run/user").glob("*/gdm/Xauthority")):
            env["XAUTHORITY"] = str(auth)
            env["XDG_RUNTIME_DIR"] = str(auth.parent)
            break
    try:
        p = subprocess.run(
            cmd, cwd=WORKDIR, shell=True, env=env, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout + "\n" + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "[timeout] Command exceeded timeout limit."


def get_system_telemetry():
    mem_total, mem_avail = 32.0, 16.0
    try:
        meminfo = Path("/proc/meminfo").read_text()
        mt = re.search(r"MemTotal:\s+(\d+)", meminfo)
        ma = re.search(r"MemAvailable:\s+(\d+)", meminfo)
        if mt and ma:
            mem_total = int(mt.group(1)) / 1048576.0
            mem_avail = int(ma.group(1)) / 1048576.0
    except Exception:
        pass
    used = mem_total - mem_avail

    temp_c = "N/A"
    for z in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            val = int(z.read_text().strip())
            if 20000 <= val <= 110000:
                temp_c = f"{val / 1000.0:.1f}°C"
                break
        except Exception:
            pass
    return f"{used:.1f} / {mem_total:.1f} GB", temp_c


def get_running_game_status():
    try:
        p = subprocess.run(
            ["pgrep", "-fa", "dm/host/build/orin_host"],
            capture_output=True, text=True, timeout=3
        )
        if p.returncode == 0 and p.stdout.strip():
            line = p.stdout.strip().splitlines()[0]
            pid = line.split()[0]
            m = re.search(r"orin_host_([0-9A-Fa-f]{16})", line)
            gid = m.group(1) if m else "active"
            return f"🟢 Running {gid} (PID {pid})"
    except Exception:
        pass
    return "⏹ No Game Running"


def get_game_log_tail(game_id, max_lines=80):
    lf = WORKDIR / f"play_{game_id}.log"
    if not lf.exists():
        return f"(No log file play_{game_id}.log yet)"
    try:
        lines = lf.read_text(errors="replace").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception as e:
        return f"[error reading log: {e}]"


def auto_optimize_orin_environment():
    STATE.log("[Auto-Prep] Purging Ubuntu desktop RAM hogs & keeping DISPLAY=:0 awake...")
    cmds = (
        "pkill -f chromium || true; pkill -f firefox || true; "
        "pkill -f gnome-software || true; pkill -f evolution || true; pkill -f tracker-miner || true; "
        "export DISPLAY=:0; xset -display :0 s off -dpms s noblank 2>/dev/null || true"
    )
    run_cmd(cmds, timeout=15)


def enter_eco_sleep():
    if not STATE.eco_sleeping:
        STATE.eco_sleeping = True
        STATE.active_model = f"🌙 15W Eco Sleep (Wakes {STATE.vac_start})"
        STATE.log(f"[Vacation Mode] Entering 15W Low-Power Eco Sleep until {STATE.vac_start}...")
        run_cmd("sudo /usr/local/bin/orin-power-mode eco", timeout=20)


def wake_super_mode():
    STATE.eco_sleeping = False
    STATE.log("[Power] Waking Orin into MAXN Super Mode...")
    run_cmd("sudo /usr/local/bin/orin-power-mode super", timeout=20)


def is_in_window(now_hm, start_hm, end_hm):
    if start_hm <= end_hm:
        return start_hm <= now_hm < end_hm
    return now_hm >= start_hm or now_hm < end_hm


def discover_games():
    games = dict(KNOWN_GAMES)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    for p in list(RAW_DIR.iterdir()) + list(PACKS_DIR.iterdir()):
        gid = p.stem if p.suffix == ".nup" else p.name.split("_")[0].split("-")[0]
        if re.match(r"^0[14]00[0-9A-Fa-f]{12}$", gid) and gid not in games:
            games[gid] = f"Custom Title ({gid})"

    result = []
    for gid, title in sorted(games.items()):
        packed = (PACKS_DIR / gid / "main.patched.bin").exists()
        built = (WORKDIR / f"dm/host/build/orin_host_{gid}").exists()
        raw_exists = (
            (RAW_DIR / f"{gid}.nup").exists()
            or (RAW_DIR / f"{gid}_merged" / "romfs").exists()
            or (RAW_DIR / gid).exists()
        )
        upd_tag = ""
        upd_dir = RAW_DIR / gid / "updates"
        if upd_dir.is_dir():
            upds = sorted([d.name for d in upd_dir.iterdir() if d.is_dir()])
            if upds:
                upd_tag = f" (upd {upds[-1]})"
        if built and packed:
            status = f"🎮 Ready{upd_tag}"
        elif packed:
            status = f"✅ Packed, Needs Host Build{upd_tag}"
        elif raw_exists:
            status = f"📦 Raw Ready{upd_tag}"
        else:
            status = "⚠️ No Backup"
        result.append({"id": gid, "label": f"{gid} — {title} [{status}]"})
    return result


def execute_carbon_tool(name, args):
    try:
        if name == "run_bash":
            cmd = args.get("command", "")
            STATE.log(f"  [Carbon $] {cmd[:110]}")
            rc, out = run_cmd(cmd, timeout=1200)
            for l in out.splitlines()[-4:]:
                if l.strip():
                    STATE.log(f"    -> {l.strip()[:110]}")
            return f"exit_code={rc}\n{out[-8000:]}"

        elif name == "read_file":
            rel = args.get("path", "")
            fp = (WORKDIR / rel).resolve()
            if not fp.exists() or not fp.is_file():
                return f"[error] File not found: {rel}"
            lines = fp.read_text(errors="replace").splitlines()
            s = max(1, int(args.get("start_line") or 1))
            e = min(len(lines), int(args.get("end_line") or min(len(lines), s + 500)))
            STATE.log(f"  [Carbon Read] {rel} (lines {s}..{e} of {len(lines)})")
            numbered = [f"{i}: {lines[i-1]}" for i in range(s, e + 1)]
            return f"File {rel} (total {len(lines)} lines):\n" + "\n".join(numbered)

        elif name == "write_file":
            rel = args.get("path", "")
            content = args.get("content", "")
            fp = (WORKDIR / rel).resolve()
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
            STATE.log(f"  [Carbon Write] Wrote {rel} ({len(content)} bytes)")
            return f"Successfully wrote {rel} ({len(content)} bytes)."

        elif name == "patch_file":
            rel = args.get("path", "")
            search = args.get("search", "")
            replace = args.get("replace", "")
            fp = (WORKDIR / rel).resolve()
            if not fp.exists():
                return f"[error] File not found: {rel}"
            text = fp.read_text(errors="replace")
            if search in text:
                fp.write_text(text.replace(search, replace, 1))
                STATE.log(f"  [Carbon Patch] Patched {rel}")
                return f"Successfully patched {rel}."
            s_lines = [l.strip() for l in search.splitlines()]
            c_lines = text.splitlines()
            for idx in range(len(c_lines) - len(s_lines) + 1):
                if [l.strip() for l in c_lines[idx : idx + len(s_lines)]] == s_lines:
                    new_lines = c_lines[:idx] + replace.splitlines() + c_lines[idx + len(s_lines) :]
                    fp.write_text("\n".join(new_lines) + "\n")
                    STATE.log(f"  [Carbon Patch] Patched {rel} (whitespace-normalized)")
                    return f"Successfully patched {rel} (whitespace-normalized)."
            return f"[error] search block not found in {rel}. Read the file lines first to get exact text."
    except Exception as e:
        return f"[tool exception] {e}"
    return f"[error] Unknown tool {name}"


def carbon_autopilot_loop():
    try:
        wake_super_mode()
        auto_optimize_orin_environment()
        run_cmd("pkill -x llama-server || true", timeout=10)

        api_key = STATE.api_key or os.environ.get("GEMINI_API_KEY", "")
        if not api_key or api_key == "PASTE_YOUR_GEMINI_API_KEY_HERE":
            STATE.log("[Error] No GEMINI_API_KEY saved! Paste and save your key in the Web UI first.")
            STATE.running = False
            return

        if MISSION_FILE.exists():
            file_prompt = MISSION_FILE.read_text(errors="replace").strip()
            if file_prompt:
                STATE.mission_prompt = file_prompt

        sys_instruction = (
            "You are the autonomous Lead Systems Engineer for OrinNative (Switch 2 native Linux aarch64 port) running directly on a "
            "Jetson AGX Orin 32GB (Ubuntu 24.04 aarch64, L4T 595.78 Ampere Vulkan 1.4, SDL3, .NET 10 at /usr/local/bin/dotnet). "
            "You have direct tool access (run_bash, read_file, write_file, patch_file) inside ~/orin-switch2/orin-native.\n\n"
            "CURRENT ARCHITECTURE & VERIFIED BASELINE:\n"
            f"1. Active Selected Game: {STATE.game_id} ({KNOWN_GAMES.get(STATE.game_id, STATE.game_id)}).\n"
            "2. Core Linux/Orin Port Status: COMPLETE & VERIFIED at 60 FPS in Workload-A 3D Engine (0400000000010000).\n"
            "   - Platform/Frontend: `dm/host/plat/plat_linux.c`, `dm/host/front_linux.c` (SDL3 Vulkan + X11 InputFocus), `dm/host/movie_linux.c`, `dm/host/Makefile`.\n"
            "   - Vulkan Renderer: `dm/gfx/vk_android/*.c` (`BCn=hw`, `VK_EXT_mesh_shader`, `memfd` alias, `DM_VK_POOL_CACHED=1`, "
            "target 10 2D-row texel buffers, `vka_ixc_spv.h` indirect quad expansion, `vka_draw.c` colorWriteMask fix in commit `44a2998e`).\n"
            "3. Standard Tooling & Commands:\n"
            "   - `./launch_game.sh build <GAME_ID>`: Compile `dm/host/build/orin_host_<GAME_ID>`.\n"
            "   - `./launch_game.sh prep <GAME_ID>`: Compile binary + pack NSOs & Vulkan shaders (`packs/<GAME_ID>` and `packs/<GAME_ID>-shaders`).\n"
            "   - `./launch_game.sh play <GAME_ID>`: Boot game on `DISPLAY=:0` (logs to `play_<GAME_ID>.log`).\n"
            "   - `./launch_game.sh shot`: Capture live frame via `DM_VK_CTL=/tmp/dm_ctl.txt` -> `boot_screen.png`.\n"
            "   - `./launch_game.sh stop`: Stop running game.\n"
            "4. Guardrails: Never break Workload-A (`0400000000010000`) or overwrite our Linux/Orin fixes (`44a2998e`, `b2e857dd`)."
        )

        active_prompt = STATE.mission_prompt.replace("<GAME_ID>", STATE.game_id)

        messages = [{
            "role": "user",
            "parts": [{"text": active_prompt}],
        }]

        STATE.log("🤖 Started Carbon Autopilot (Full Agentic Tool Loop)!")
        max_turns = 120

        for turn in range(1, max_turns + 1):
            if STATE.stop_requested:
                break
            if STATE.spend_usd >= STATE.budget_usd:
                STATE.log(f"[Smart Budget] Reached ${STATE.spend_usd:.2f} / ${STATE.budget_usd:.2f} cap. Halting Autopilot.")
                break

            # Compact only large bash/file outputs older than 25 turns (~50 messages)
            if len(messages) > 52:
                for old_m in messages[:-50]:
                    for pt in old_m.get("parts", []):
                        fr = pt.get("functionResponse", {}).get("response", {})
                        res_txt = fr.get("result", "")
                        if len(res_txt) > 1600:
                            fr["result"] = res_txt[:900] + "\n...[older output trimmed]...\n" + res_txt[-500:]

            time.sleep(2)
            sticky_until = getattr(STATE, "_argon_sticky_until", 0)
            if STATE.preferred_model == "argon" or (STATE.preferred_model == "auto" and turn <= sticky_until):
                primary_m = "gemini-4-argon"
                fallback_m = "gemini-4-carbon"
            else:
                primary_m = "gemini-4-carbon"
                fallback_m = "gemini-4-argon"

            STATE.active_model = f"🤖 {primary_m.split('-')[-1].capitalize()} (Turn {turn}/{max_turns})"
            payload = {
                "systemInstruction": {"parts": [{"text": sys_instruction}]},
                "contents": messages,
                "tools": CARBON_TOOLS,
                "generationConfig": {
                    "maxOutputTokens": 32768,
                    "thinkingConfig": {"thinkingLevel": "LOW"},
                },
            }

            data = None
            for attempt_m in ([primary_m, fallback_m] if STATE.preferred_model == "auto" else [primary_m]):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{attempt_m}:generateContent?key={api_key}"
                try:
                    req = urllib.request.Request(
                        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=75) as resp:
                        data = json.loads(resp.read().decode())
                    STATE.active_model = f"🤖 {attempt_m.split('-')[-1].capitalize()} (Turn {turn}/{max_turns})"
                    break
                except Exception as e:
                    STATE.log(f"[{attempt_m} API Error] {e}")
                    if STATE.preferred_model == "auto" and attempt_m == "gemini-4-carbon":
                        STATE._argon_sticky_until = turn + 10
                        STATE.log(f"  -> Sticky-switching to gemini-4-argon for next 10 turns...")
                    time.sleep(2)
            if data is None:
                continue

            usage = data.get("usageMetadata", {})
            in_tok = int(usage.get("promptTokenCount", 0))
            cached_tok = int(usage.get("cachedContentTokenCount", 0))
            out_tok = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
            call_cost = ((max(0, in_tok - cached_tok) * 2.0) + (cached_tok * 0.10) + (out_tok * 10.0)) / 1_000_000.0
            STATE.spend_usd += call_cost
            STATE.carbon_calls += 1
            STATE.save_kv("spend_usd", f"{STATE.spend_usd:.4f}")
            STATE.save_kv("carbon_calls", STATE.carbon_calls)

            cand = data["candidates"][0]
            content = cand.get("content", {})
            parts = content.get("parts", [])
            messages.append(content)

            fn_calls = []
            for part in parts:
                if not part.get("thought", False) and part.get("text"):
                    for line in part["text"].strip().splitlines()[:6]:
                        if line.strip():
                            STATE.log(f"[Carbon] {line.strip()[:120]}")
                if "functionCall" in part:
                    fn_calls.append(part["functionCall"])

            if not fn_calls:
                reason = cand.get("finishReason", "")
                if reason == "MAX_TOKENS":
                    STATE.log("  [Autopilot] Hit MAX_TOKENS mid-turn; prompting Carbon to split large file writes and continue...")
                    messages.append({"role": "user", "parts": [{"text": "Your previous tool call was cut off by MAX_TOKENS. Write large files in chunks (<250 lines) or use patch_file / run_bash heredoc, and continue now."}]})
                    continue
                STATE.log(f"🤖 Carbon Autopilot stopped calling tools (finishReason={reason})")
                _, sha = run_cmd("git rev-parse --short HEAD", timeout=5)
                STATE.record_event(STATE.current_stage, turn, "carbon-autopilot", f"Completed ({STATE.game_id}, ${STATE.spend_usd:.2f})", sha.strip())
                break

            resp_parts = []
            for fc in fn_calls:
                fn_name = fc["name"]
                fn_args = fc.get("args", {})
                result_str = execute_carbon_tool(fn_name, fn_args)
                resp_parts.append({
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"result": result_str},
                    }
                })

            messages.append({"role": "user", "parts": resp_parts})

            if (WORKDIR / "boot_screen.png").exists():
                _, b_out = run_cmd("identify -format '%[fx:mean]' boot_screen.png 2>/dev/null || echo 0.0", timeout=5)
                STATE.last_brightness = b_out.strip()[:6]

    except Exception as e:
        STATE.log(f"[Autopilot Fatal Error] {e}")
        for l in traceback.format_exc().splitlines()[-5:]:
            STATE.log(f"  {l}")
    finally:
        STATE.running = False
        STATE.active_model = "Idle"
        STATE.log("Carbon Autopilot stopped.")
        if STATE.vacation_mode and not is_in_window(time.strftime("%H:%M"), STATE.vac_start, STATE.vac_end):
            enter_eco_sleep()


def vacation_scheduler_loop():
    while True:
        time.sleep(20)
        if not STATE.vacation_mode:
            continue
        now_hm = time.strftime("%H:%M")
        today = time.strftime("%Y-%m-%d")
        in_win = is_in_window(now_hm, STATE.vac_start, STATE.vac_end)

        if in_win:
            STATE.manual_awake = False
        if in_win and not STATE.running and STATE.last_wake_date != today:
            STATE.last_wake_date = today
            STATE.spend_usd = 0.0
            STATE.carbon_calls = 0
            STATE.save_kv("spend_usd", "0.0")
            STATE.save_kv("carbon_calls", 0)
            STATE.log(f"[Vacation Mode] ⏰ {now_hm} Night-Shift started! Launching Carbon Autopilot...")
            STATE.stop_requested = False
            STATE.running = True
            threading.Thread(target=carbon_autopilot_loop, daemon=True).start()
        elif not in_win and not STATE.manual_awake:
            if STATE.running and not STATE.stop_requested:
                STATE.log(f"[Vacation Mode] ☀️ {now_hm} Window ended ({STATE.vac_end}). Stopping after current turn...")
                STATE.stop_requested = True
            elif not STATE.running and not STATE.eco_sleeping:
                enter_eco_sleep()


HTML_UI = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>OrinConductor v4 — Mobile Studio</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #0d1117; color: #e6edf3; margin: 0; padding: 10px; overflow-x: hidden; }
  h2 { font-size: 18px; margin: 4px 0 10px 0; }
  .stats-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 10px; }
  @media (min-width: 768px) { .stats-grid { grid-template-columns: repeat(4, 1fr); } }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 12px; margin-bottom: 10px; width: 100%; }
  .label { font-size: 10.5px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.4px; }
  .val { font-size: 14px; font-weight: 700; margin-top: 4px; color: #58a6ff; word-break: break-word; }
  .btn-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  button, select, input, textarea { background: #238636; color: #fff; border: none; padding: 10px 12px; border-radius: 8px; font-size: 13.5px; font-weight: 600; cursor: pointer; }
  button { flex: 1 1 calc(50% - 6px); text-align: center; }
  @media (min-width: 640px) { button { flex: 0 0 auto; } }
  button.secondary { background: #30363d; }
  button.blue { background: #1f6feb; }
  button.purple { background: #8957e5; }
  button.stop { background: #da3633; }
  button.tab { flex: 1; background: #21262d; border: 1px solid #30363d; padding: 8px; font-size: 13px; }
  button.tab.active { background: #1f6feb; border-color: #388bfd; }
  select, input, textarea { background: #0d1117; border: 1px solid #30363d; font-weight: 500; width: 100%; margin-top: 6px; }
  textarea { height: 150px; font-family: ui-monospace, monospace; font-size: 12px; color: #e6edf3; padding: 10px; }
  pre { background: #010409; border: 1px solid #30363d; padding: 10px; border-radius: 8px; height: 320px; overflow-y: auto; font-size: 12px; color: #7ee787; margin: 8px 0 0 0; white-space: pre-wrap; word-break: break-word; }
  pre.gamelog { color: #a5d6ff; }
  details summary { cursor: pointer; font-weight: 600; font-size: 13.5px; color: #58a6ff; padding: 4px 0; user-select: none; }
  .table-wrap { width: 100%; overflow-x: auto; }
  table { width: 100%; min-width: 480px; border-collapse: collapse; font-size: 12px; margin-top: 6px; }
  th, td { border-bottom: 1px solid #30363d; padding: 6px 8px; text-align: left; }
</style>
</head>
<body>
<h2>⚡ OrinConductor v4 — Switch 2 Studio</h2>

<div class="stats-grid">
  <div class="card" style="margin:0;"><div class="label">Autopilot / Game</div><div class="val" id="stage">-</div></div>
  <div class="card" style="margin:0;"><div class="label">Active Agent</div><div class="val" id="model">-</div></div>
  <div class="card" style="margin:0;"><div class="label">RAM & Temp</div><div class="val" id="telemetry">-</div></div>
  <div class="card" style="margin:0;"><div class="label">Spend / Brightness</div><div class="val" id="carbon">-</div></div>
</div>

<div class="card">
  <div class="label">Target Game & Quick Actions</div>
  <select id="game" onchange="selectGame()"></select>
  <select id="modelsel" onchange="selectModel()">
    <option value="auto">🔀 Model: Carbon + Argon Auto-Fallback (TPM Safe)</option>
    <option value="argon">⚡ Model: Gemini 4 Argon Only (Low TPM Usage)</option>
    <option value="carbon">🤖 Model: Gemini 4 Carbon Only</option>
  </select>
  <div class="btn-row">
    <button onclick="startRun()">▶ Start Autopilot</button>
    <button class="stop" onclick="stopRun()">⏹ Stop Autopilot</button>
    <button class="blue" onclick="gameAction('launch_selected')">🎮 Launch Game</button>
    <button class="stop" onclick="gameAction('stop_game')">🛑 Stop Game</button>
    <button class="secondary" onclick="gameAction('snap_game')">📸 Snap Frame</button>
    <button class="secondary" onclick="gameAction('build_selected')">🔄 Rebuild Binary</button>
    <button class="secondary" onclick="gameAction('prep_selected')">🔨 Full Prep</button>
    <button id="copybtn" class="purple" onclick="copyDiagBundle()">📋 Copy Log for AI</button>
  </div>
</div>

<div class="card">
  <div class="label">Latest Frame (boot_screen.png — Tap to Refresh)</div>
  <img id="screen" src="/screenshot.png" onclick="gameAction('snap_game')" style="width:100%; border-radius:8px; margin-top:8px; border:1px solid #30363d; display:block;" onerror="this.style.display='none'" onload="this.style.display='block'"/>
</div>

<div class="card">
  <div class="btn-row" style="margin-top:0;">
    <button id="tab_game" class="tab active" onclick="setLogTab('game')">🎮 Live Game Log</button>
    <button id="tab_carbon" class="tab" onclick="setLogTab('carbon')">🤖 Carbon Autopilot Log</button>
  </div>
  <div id="panel_game">
    <pre class="gamelog" id="gamelog"></pre>
  </div>
  <div id="panel_carbon" style="display:none;">
    <pre id="logs"></pre>
  </div>
</div>

<div class="card">
  <details>
    <summary>📝 Edit Carbon Mission Prompt & Presets</summary>
    <select id="preset" onchange="loadPreset()">
      <option value="">-- Load Mission Preset --</option>
      <option value="mkw_hang">🏎️ Workload-B (0400000000020000): Fix GPU Hang on Title Screen & Enter Gameplay</option>
      <option value="mkw_boot">🏎️ Workload-B (0400000000020000): Full Prep, Build, Pack & Boot</option>
      <option value="triage_selected">🛠️ Selected Game: Prep, Boot & Triage Crashes/HLE</option>
      <option value="upstream_sync">🔄 Safe Upstream Sync (UpstreamRuntime + upstream-core)</option>
      <option value="audit_codebase">🧹 Audit & Clean Up AI-Patched Host/Vulkan Files</option>
    </select>
    <textarea id="mission_box" placeholder="Enter Carbon Autopilot mission instructions..."></textarea>
    <div class="btn-row">
      <button class="blue" onclick="saveMission()">💾 Save Mission Prompt</button>
    </div>
  </details>
</div>

<div class="card">
  <details>
    <summary>⚙️ Power, Vacation Mode, Shell & API Key</summary>
    <div class="btn-row">
      <button id="vacbtn" class="secondary" onclick="toggleVacation()">🏖️ Vacation Mode</button>
      <button class="secondary" onclick="action('wake_now')">☀️ Wake (Super)</button>
      <button class="secondary" onclick="action('sleep_now')">💤 15W Eco Sleep</button>
      <button class="secondary" onclick="action('reset_carbon')">🔄 Reset $0.00</button>
      <button class="stop" onclick="if(confirm('Safely shut down the Orin now?')) action('poweroff_orin')">⏻ Shutdown</button>
    </div>
    <input type="text" id="shellcmd" placeholder="Run shell command in orin-native..."/>
    <div class="btn-row"><button class="secondary" onclick="runShell()">💻 Run Shell Command</button></div>
    <input type="password" id="apikey" placeholder="Gemini API Key (SQLite)"/>
    <div class="btn-row"><button class="secondary" onclick="saveKey()">💾 Save API Key</button></div>
  </details>
</div>

<div class="card">
  <details>
    <summary>📜 Run History (SQLite)</summary>
    <div class="table-wrap">
      <table id="hist"><thead><tr><th>Time</th><th>Stg</th><th>Turn</th><th>Agent</th><th>Summary</th><th>SHA</th></tr></thead><tbody></tbody></table>
    </div>
  </details>
</div>

<script>
let initialized = false;
let presetsMap = {};
function setLogTab(t) {
  document.getElementById('panel_game').style.display = (t === 'game') ? 'block' : 'none';
  document.getElementById('panel_carbon').style.display = (t === 'carbon') ? 'block' : 'none';
  document.getElementById('tab_game').className = (t === 'game') ? 'tab active' : 'tab';
  document.getElementById('tab_carbon').className = (t === 'carbon') ? 'tab active' : 'tab';
}
async function refresh() {
  const r = await fetch('/api/state');
  const d = await r.json();
  presetsMap = d.presets || {};
  document.getElementById('stage').textContent = (d.running ? "🟢 AUTO | " : "") + d.game_proc;
  document.getElementById('model').textContent = d.active_model;
  document.getElementById('telemetry').textContent = d.ram + " | " + d.temp;
  document.getElementById('carbon').textContent = d.spend_usd + " (" + d.carbon_calls + ") | Br:" + d.last_brightness;
  const vb = document.getElementById('vacbtn');
  if (vb) {
    vb.textContent = d.vacation_mode ? `🏖️ Night-Shift: ON (${d.vac_start}→${d.vac_end})` : `🏖️ Night-Shift: OFF`;
    vb.style.background = d.vacation_mode ? '#1f6feb' : '#30363d';
  }
  const pre = document.getElementById('logs');
  pre.textContent = d.logs.join(String.fromCharCode(10));
  pre.scrollTop = pre.scrollHeight;

  document.getElementById('tab_game').textContent = `🎮 Game Log (${d.game_id.slice(0,6)}..)`;
  const gpre = document.getElementById('gamelog');
  gpre.textContent = d.game_log;
  gpre.scrollTop = gpre.scrollHeight;

  document.getElementById('screen').src = '/screenshot.png?t=' + Date.now();

  const msel = document.getElementById('modelsel');
  if (msel && d.preferred_model) msel.value = d.preferred_model;
  const sel = document.getElementById('game');
  const cur = sel.value || d.game_id;
  sel.innerHTML = d.games.map(g => `<option value="${g.id}">${g.label}</option>`).join('');
  sel.value = cur;

  if (!initialized) {
    document.getElementById('mission_box').value = d.mission_prompt || '';
    initialized = true;
  }

  document.querySelector('#hist tbody').innerHTML = d.history.map(h =>
    `<tr><td>${h[0]}</td><td>S${h[1]}</td><td>${h[2]}</td><td>${h[3]}</td><td>${h[4]}</td><td><code>${h[5]}</code></td></tr>`
  ).join('');
}
async function selectModel() {
  const m = document.getElementById('modelsel').value;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'select_model', model:m})});
  refresh();
}
async function selectGame() {
  const game = document.getElementById('game').value;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'select_game', game_id:game})});
  refresh();
}
function loadPreset() {
  const k = document.getElementById('preset').value;
  if (k && presetsMap[k]) {
    document.getElementById('mission_box').value = presetsMap[k];
  }
}
async function saveMission() {
  const m = document.getElementById('mission_box').value;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'save_mission', mission:m})});
  refresh();
}
async function startRun() {
  const game = document.getElementById('game').value;
  const mission = document.getElementById('mission_box').value;
  setLogTab('carbon');
  await fetch('/api/start', {method:'POST', body:JSON.stringify({game_id:game, mission:mission})});
  refresh();
}
async function gameAction(act) {
  const game = document.getElementById('game').value;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:act, game_id:game})});
  refresh();
}
async function stopRun() { await fetch('/api/stop', {method:'POST'}); refresh(); }
async function toggleVacation() { await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'toggle_vacation'})}); refresh(); }
async function action(act) { await fetch('/api/action', {method:'POST', body:JSON.stringify({action:act})}); refresh(); }
async function runShell() {
  const c = document.getElementById('shellcmd').value;
  if (!c) return;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'run_shell', cmd:c})});
  document.getElementById('shellcmd').value = '';
  refresh();
}
async function copyDiagBundle() {
  const btn = document.getElementById('copybtn');
  const game = document.getElementById('game').value;
  btn.textContent = '⏳ Copying...';
  const r = await fetch('/api/diag?game_id=' + encodeURIComponent(game));
  const d = await r.json();
  const txt = d.bundle;
  try {
    await navigator.clipboard.writeText(txt);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
  btn.textContent = '✅ Copied!';
  setTimeout(() => { btn.textContent = '📋 Copy Log for AI'; }, 3000);
}
async function saveKey() {
  const k = document.getElementById('apikey').value;
  await fetch('/api/action', {method:'POST', body:JSON.stringify({action:'save_key', key:k})});
  document.getElementById('apikey').value = '';
  refresh();
}
setInterval(refresh, 3000); refresh();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_UI.encode())
        elif self.path.startswith("/screenshot.png"):
            img = WORKDIR / "boot_screen.png"
            if img.exists():
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                self.wfile.write(img.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path.startswith("/api/diag"):
            gid = STATE.game_id
            if "game_id=" in self.path:
                gid = self.path.split("game_id=")[-1].split("&")[0]
            _, git_info = run_cmd("git status -s && echo '---' && git log -n 5 --oneline", timeout=5)
            build_tail = ""
            bf = WORKDIR / "build.log"
            if bf.exists():
                build_tail = "\n".join(bf.read_text(errors="replace").splitlines()[-30:])
            bundle = "\n".join([
                f"### OrinNative Diagnostic Bundle ({gid})",
                f"- **Status:** AutopilotRunning={STATE.running} | GameProc={get_running_game_status()} | Spend=${STATE.spend_usd:.2f}",
                "#### Git State (`git status -s` & `git log -n 5`)",
                "```text",
                git_info.strip(),
                "```",
                "#### Conductor / Carbon Log (Last 50 lines)",
                "```text",
                "\n".join(STATE.logs[-50:]),
                "```",
                f"#### Game Log Highlights (`play_{gid}.log` [RED] / Errors / Non-Slot qtrace)",
                "```text",
                run_cmd(f"grep -E '\[RED\]|hang|fault|ERROR|WARN|qtrace' play_{gid}.log | grep -v -E 'pool table slot|completion: event|\[longframe\]' | tail -n 65", timeout=5)[1] or "(none)",
                "```",
                f"#### Game Log (`play_{gid}.log`, Last 65 lines)",
                "```text",
                run_cmd(f"grep -v -E 'pool table slot|completion: event|\[longframe\]' play_{gid}.log | tail -n 45", timeout=5)[1],
                "```",
                "#### Build Log (`build.log`, Last 30 lines)",
                "```text",
                build_tail or "(empty)",
                "```",
            ])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"bundle": bundle}).encode())
        elif self.path.startswith("/api/state"):
            with sqlite3.connect(DB_PATH) as conn:
                hist = conn.execute(
                    "SELECT ts, stage, strike, model, summary, commit_hash FROM history ORDER BY id DESC LIMIT 20"
                ).fetchall()
            ram, temp = get_system_telemetry()
            payload = {
                "running": STATE.running,
                "game_id": STATE.game_id,
                "game_proc": get_running_game_status(),
                "active_model": STATE.active_model,
                "preferred_model": STATE.preferred_model,
                "carbon_calls": STATE.carbon_calls,
                "spend_usd": f"${STATE.spend_usd:.2f} / ${STATE.budget_usd:.2f}",
                "last_brightness": STATE.last_brightness,
                "vacation_mode": STATE.vacation_mode,
                "vac_start": STATE.vac_start,
                "vac_end": STATE.vac_end,
                "ram": ram,
                "temp": temp,
                "games": discover_games(),
                "mission_prompt": STATE.mission_prompt,
                "presets": PRESET_MISSIONS,
                "logs": STATE.logs[-120:],
                "game_log": get_game_log_tail(STATE.game_id),
                "history": hist,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode() or "{}")
        if self.path == "/api/start":
            if not STATE.running:
                STATE.manual_awake = True
                if body.get("game_id"):
                    STATE.game_id = body["game_id"]
                    STATE.save_kv("game_id", STATE.game_id)
                if body.get("mission"):
                    STATE.set_mission(body["mission"])
                STATE.stop_requested = False
                STATE.running = True
                threading.Thread(target=carbon_autopilot_loop, daemon=True).start()
        elif self.path == "/api/stop":
            STATE.stop_requested = True
            STATE.log("Stop requested by user; will halt after current tool call finishes.")
        elif self.path == "/api/action":
            act = body.get("action")
            if act == "select_model":
                m = body.get("model", "auto")
                STATE.preferred_model = m
                STATE.save_kv("preferred_model", m)
                STATE.log(f"Switched Autopilot model mode to: {m}")
            elif act == "select_game":
                gid = body.get("game_id", STATE.game_id)
                STATE.game_id = gid
                STATE.save_kv("game_id", gid)
            elif act == "save_mission":
                STATE.set_mission(body.get("mission", ""))
                STATE.log("💾 Saved updated Carbon Autopilot mission prompt.")
            elif act == "toggle_vacation":
                STATE.vacation_mode = not STATE.vacation_mode
                STATE.save_kv("vacation_mode", STATE.vacation_mode)
                STATE.last_wake_date = ""
                STATE.log(f"[Vacation Mode] Night-Shift (01:30 -> 08:30) set to: {STATE.vacation_mode}")
            elif act == "wake_now":
                STATE.manual_awake = True
                STATE.active_model = "☀️ Awake (Manual Super Mode)"
                threading.Thread(target=wake_super_mode, daemon=True).start()
            elif act == "sleep_now":
                STATE.manual_awake = False
                STATE.stop_requested = True
                threading.Thread(target=enter_eco_sleep, daemon=True).start()
            elif act == "reset_carbon":
                STATE.carbon_calls = 0
                STATE.spend_usd = 0.0
                STATE.save_kv("carbon_calls", 0)
                STATE.save_kv("spend_usd", "0.0")
                STATE.log("[Smart Budget] Reset spend counter back to $0.00 / $3.00.")
            elif act == "run_shell":
                cmd_str = body.get("cmd", "").strip()
                if cmd_str:
                    STATE.log(f"$ {cmd_str}")
                    threading.Thread(target=lambda: STATE.log(run_cmd(cmd_str, timeout=300)[1][-1500:]), daemon=True).start()
            elif act == "poweroff_orin":
                STATE.log("[System] Safe shutdown requested. Powering off in 3 seconds...")
                threading.Thread(target=lambda: (time.sleep(3), run_cmd("sudo poweroff")), daemon=True).start()
            elif act in ("launch_selected", "prep_selected", "build_selected", "stop_game", "snap_game"):
                gid = body.get("game_id", STATE.game_id)
                STATE.game_id = gid
                STATE.save_kv("game_id", gid)
                def _run_l(a=act, g=gid):
                    if a == "launch_selected":
                        STATE.manual_awake = True
                        STATE.active_model = f"🎮 Playing {g}"
                        STATE.log(f"[Launcher] Launching {g}...")
                        rc, out = run_cmd(f"./launch_game.sh play {g}", timeout=1800)
                        STATE.log(out[-800:] if out else f"Launch exit={rc}")
                    elif a == "prep_selected":
                        STATE.manual_awake = True
                        STATE.active_model = f"🔨 Building/Packing {g}"
                        STATE.log(f"[Launcher] Building & packing {g}...")
                        rc, out = run_cmd(f"./launch_game.sh prep {g}", timeout=1800)
                        STATE.log(out[-800:] if out else f"Prep exit={rc}")
                        STATE.active_model = "Idle"
                    elif a == "build_selected":
                        STATE.manual_awake = True
                        STATE.active_model = f"🔄 Compiling {g}"
                        STATE.log(f"[Launcher] Recompiling host binary for {g}...")
                        rc, out = run_cmd(f"./launch_game.sh build {g}", timeout=600)
                        STATE.log(out[-800:] if out else f"Build exit={rc}")
                        STATE.active_model = "Idle"
                    elif a == "stop_game":
                        rc, out = run_cmd("./launch_game.sh stop", timeout=15)
                        STATE.log(out or "Game stopped.")
                        STATE.active_model = "Idle"
                    elif a == "snap_game":
                        rc, out = run_cmd("./launch_game.sh shot", timeout=10)
                        STATE.log(out or "Requested live screenshot.")
                threading.Thread(target=_run_l, daemon=True).start()
            elif act == "save_key":
                STATE.api_key = body.get("key", "").strip()
                STATE.save_kv("api_key", STATE.api_key)
                STATE.log("Saved Gemini API key to SQLite.")
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=vacation_scheduler_loop, daemon=True).start()
    print(f"🚀 OrinConductor v4 Web UI running on http://0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
