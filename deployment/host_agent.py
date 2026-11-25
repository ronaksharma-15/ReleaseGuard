"""Linux VM helper. Only this process can invoke Docker on behalf of the controller."""

import json
import os
import re
import socketserver
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import urlopen


PROJECT = Path("/opt/releaseguard")
STATE = Path("/var/lib/releaseguard-agent")
SLOTS_FILE = STATE / "slots.env"
ROUTE_FILE = STATE / "nginx" / "route.conf"
SOCKET_PATH = Path("/run/releaseguard/agent.sock")
SLOTS = ("blue", "green")


def atomic_write(path, content, mode=0o600):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="ascii") as handle:
        temp = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.chmod(mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def weights(stable, percent):
    if stable not in SLOTS or type(percent) is not int or percent not in (0, 10, 50, 100):
        raise ValueError("Invalid route.")
    return {slot: (100 - percent if slot == stable else percent) for slot in SLOTS}


def nginx_config(stable, percent):
    distribution = weights(stable, percent)
    upstreams = "\n".join(
        f"    server {slot}:8001 weight={weight} max_fails=0;"
        for slot, weight in distribution.items() if weight
    )
    uid = uuid.uuid4().hex
    return f"""# generation {uid}
upstream checkout {{
    zone checkout 64k;
{upstreams}
}}
server {{
    listen 8080;
    client_max_body_size 16k;
    location = /_releaseguard/route-id {{
        default_type text/plain;
        return 200 "{uid}";
    }}
    location / {{
        proxy_next_upstream off;
        proxy_connect_timeout 2s;
        proxy_read_timeout 5s;
        proxy_pass http://checkout;
    }}
}}
"""


def compose(*args, timeout=90):
    # This file and the trusted Compose project are host-owned.
    command = ["docker", "compose", "--project-directory", str(PROJECT), "--env-file",
               str(PROJECT / ".env"), "--env-file", str(SLOTS_FILE), "-f", str(PROJECT / "compose.yaml")]
    return subprocess.run(command + list(args), check=True, capture_output=True, text=True, timeout=timeout).stdout


def current_slot(slot):
    container = compose("ps", "-q", slot).strip()
    if not container:
        return None
    config = json.loads(subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config}}", container],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout)
    env = dict(entry.split("=", 1) for entry in config["Env"] if "=" in entry)
    return {"digest": config["Image"], "version": env.get("VERSION")}


def confirm_route(content):
    expected = content.splitlines()[0].split()[-1]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8080/_releaseguard/route-id", timeout=1) as reply:
                if reply.read(128).decode() == expected:
                    return
        except OSError:
            pass
        time.sleep(0.15)
    raise RuntimeError("NGINX did not confirm the expected routing generation.")


def apply_route(stable, percent):
    content = nginx_config(stable, percent)
    previous = ROUTE_FILE.read_text()
    try:
        atomic_write(ROUTE_FILE, content, 0o644)
        compose("exec", "-T", "router", "nginx", "-t")
        compose("exec", "-T", "router", "nginx", "-s", "reload")
        confirm_route(content)
    except Exception:
        atomic_write(ROUTE_FILE, previous, 0o644)
        compose("exec", "-T", "router", "nginx", "-t")
        compose("exec", "-T", "router", "nginx", "-s", "reload")
        confirm_route(previous)
        raise


def serving(config):
    return re.findall(r"^    server (blue|green):8001 weight=\d+ ", config, re.MULTILINE)


def validate_artifact(repo, digest, version, scenario):
    if not isinstance(digest, str) or not re.fullmatch(re.escape(repo) + r"@sha256:[a-f0-9]{64}", digest):
        raise ValueError("The image must be an immutable digest from the configured repository.")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
        raise ValueError("Invalid checkout version.")
    if scenario not in ("healthy", "wrong_total", "slow"):
        raise ValueError("Unknown scenario.")


def prepare(slot, digest, version, scenario):
    validate_artifact(os.environ["RG_IMAGE_REPOSITORY"], digest, version, scenario)
    config = ROUTE_FILE.read_text()
    confirm_route(config)
    if slot not in SLOTS or slot in serving(config):
        raise ValueError("The requested slot must be idle before it can be replaced.")
    prefix = "RG_" + slot.upper() + "_"
    original = SLOTS_FILE.read_text()
    entries = [line for line in original.splitlines() if not line.startswith(prefix)]
    entries.extend([f"{prefix}DIGEST={digest}", f"{prefix}VERSION={version}", f"{prefix}SCENARIO={scenario}"])
    atomic_write(SLOTS_FILE, "\n".join(entries) + "\n")
    try:
        compose("up", "-d", "--no-deps", "--pull", "missing", "--wait", "--wait-timeout", "40", slot, timeout=180)
        if current_slot(slot) != {"digest": digest, "version": version}:
            raise RuntimeError("The staged container does not match the requested digest and version.")
    except Exception:
        atomic_write(SLOTS_FILE, original)
        raise


def dispatch(message):
    op = message["op"]
    if op == "slots":
        return {"slots": {slot: current_slot(slot) for slot in SLOTS}, "serving": serving(ROUTE_FILE.read_text())}
    if op == "prepare":
        prepare(message["slot"], message["digest"], message["version"], message["scenario"])
    elif op == "route":
        apply_route(message["stable"], message["percent"])
    else:
        raise ValueError("Unknown operation.")
    return {"ok": True}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        try:
            line = self.rfile.readline(8193)
            if len(line) > 8192 or not line.endswith(b"\n"):
                raise ValueError("Malformed request.")
            result = dispatch(json.loads(line))
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            result = {"error": str(exc)}
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Host operation failed: {type(exc).__name__}: {exc}", flush=True)
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                print(exc.stderr.strip(), flush=True)
            result = {"error": "VM operation failed. Inspect journalctl -u releaseguard-agent on the host."}
        self.wfile.write(json.dumps(result).encode() + b"\n")


def main():
    ROUTE_FILE.parent.mkdir(exist_ok=True, parents=True)
    if not ROUTE_FILE.exists():
        atomic_write(ROUTE_FILE, nginx_config("blue", 0), 0o644)
    if not SLOTS_FILE.exists():
        atomic_write(SLOTS_FILE, "")
    SOCKET_PATH.parent.mkdir(exist_ok=True, parents=True)
    SOCKET_PATH.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(SOCKET_PATH), Handler) as server:
        os.chown(SOCKET_PATH, 0, 10001)
        SOCKET_PATH.chmod(0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
