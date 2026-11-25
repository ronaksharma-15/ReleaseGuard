"""Blue/green rollout adapter used by the controller on the Linux VM."""

import json
import socket
import time
from urllib.error import URLError
from urllib.request import urlopen


SOCKET_PATH = "/run/releaseguard/agent.sock"


def ask_agent(**message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(240)
        connection.connect(SOCKET_PATH)
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(32768))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def verify_version(url, version):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/health", timeout=2) as response:
                if response.status == 200 and response.headers.get("X-Release-Version") == version:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Readiness check did not confirm version {version}.")


class ComposeRuntime:
    router_url = "http://router:8080"

    def __init__(self):
        self.stable_slot = "blue"
        self.stable_version = None
        self.candidate_version = None

    @property
    def candidate_slot(self):
        return "green" if self.stable_slot == "blue" else "blue"

    @property
    def stable_url(self):
        return f"http://{self.stable_slot}:8001"

    @property
    def candidate_url(self):
        return f"http://{self.candidate_slot}:8001"

    def initialize(self, stable):
        info = ask_agent(op="slots")
        self.stable_version = stable["version"]
        for slot, running in info["slots"].items():
            if running and (running["digest"], running["version"]) == (stable["digest"], stable["version"]):
                self.stable_slot = slot
                break
        else:
            # Stage the recorded stable image without replacing a serving slot.
            free = set(info["slots"]) - set(info["serving"])
            if not free:
                raise RuntimeError("Recorded stable image is missing and both slots are serving. Inspect the VM.")
            self.stable_slot = sorted(free)[0]
            ask_agent(op="prepare", slot=self.stable_slot, version=stable["version"],
                      digest=stable["digest"], scenario=stable.get("scenario", "healthy"))
        verify_version(self.stable_url, self.stable_version)
        self.route(0)

    def prepare(self, release):
        self.candidate_version = release["candidate_version"]
        ask_agent(op="prepare", slot=self.candidate_slot, digest=release["candidate_digest"],
                  version=self.candidate_version, scenario=release["scenario"])
        verify_version(self.candidate_url, self.candidate_version)

    def route(self, percent):
        ask_agent(op="route", stable=self.stable_slot, percent=percent)
        if percent == 0:
            verify_version(self.router_url, self.stable_version)
        elif percent == 100:
            verify_version(self.router_url, self.candidate_version)

    def promote(self):
        self.route(100)
        self.stable_slot = self.candidate_slot
        self.stable_version = self.candidate_version
        self.candidate_version = None

    def close(self):
        pass
