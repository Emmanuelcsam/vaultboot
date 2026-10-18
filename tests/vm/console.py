#!/usr/bin/env python3
"""Bounded QEMU console for candidate-image checks; no TCP listeners.

Run in a disposable test environment. Commands in commands.jsonl act inside
the guest or its QMP monitor; responses and screenshots stay in the test folder.
The original image is read-only and all guest writes use a qcow2 overlay.
"""
import argparse
import base64
import json
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time


class VM:
    def __init__(self, image, work, firmware="bios", offline=False, memory=2048):
        self.work = Path(work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        image = Path(image).resolve()
        self.serial_path = self.work / "serial.sock"
        self.qmp_path = self.work / "qmp.sock"
        overlay = self.work / "disk.qcow2"
        if not overlay.exists():
            subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(image), str(overlay)], check=True)
        args = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-smp", "2", "-m", str(memory),
                "-drive", f"file={overlay},format=qcow2,if=virtio", "-vga", "std",
                "-display", "none", "-serial", f"unix:{self.serial_path},server=on,wait=off",
                "-qmp", f"unix:{self.qmp_path},server=on,wait=off", "-no-reboot"]
        args += ["-nic", "none" if offline else "user,model=virtio-net-pci"]
        if firmware == "uefi":
            code = next(Path("/usr/share/OVMF").glob("OVMF_CODE_4M.fd"))
            variables = self.work / "OVMF_VARS.fd"
            if not variables.exists():
                shutil.copy2(code.with_name("OVMF_VARS_4M.fd"), variables)
            args += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                     "-drive", f"if=pflash,format=raw,file={variables}"]
        self.process = subprocess.Popen(args, stdout=(self.work / "qemu.log").open("ab"), stderr=subprocess.STDOUT)
        self.serial = self.connect(self.serial_path)
        self.qmp_socket = self.connect(self.qmp_path)
        self.qmp_stream = self.qmp_socket.makefile("rwb", buffering=0)
        self.qmp_stream.readline()
        self.qmp("qmp_capabilities")
        self.log = (self.work / "serial.log").open("ab", buffering=0)
        self.buffer = b""

    @staticmethod
    def connect(path):
        for _ in range(100):
            connection = socket.socket(socket.AF_UNIX)
            try:
                connection.connect(str(path))
                return connection
            except (FileNotFoundError, ConnectionRefusedError):
                connection.close()
                time.sleep(0.1)
        raise TimeoutError(f"QEMU socket did not appear: {path}")

    def qmp(self, command, arguments=None):
        self.qmp_stream.write(json.dumps({"execute": command, "arguments": arguments or {}}).encode() + b"\n")
        while True:
            result = json.loads(self.qmp_stream.readline())
            if "error" in result:
                raise RuntimeError(result)
            if "return" in result:
                return result["return"]

    def read_until(self, marker, timeout=180):
        deadline = time.monotonic() + timeout
        while marker not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Guest did not emit {marker!r}; tail: {self.buffer[-2000:]!r}")
            if not select.select([self.serial], [], [], 1)[0]:
                continue
            chunk = self.serial.recv(65536)
            if not chunk:
                raise EOFError("Guest serial console closed")
            self.log.write(chunk)
            self.buffer += chunk
        end = self.buffer.index(marker) + len(marker)
        result, self.buffer = self.buffer[:end], self.buffer[end:]
        return result.decode("utf-8", "replace")

    def login(self):
        self.read_until(b"login:", 300)
        self.serial.sendall(b"writer\n")
        self.read_until(b"Password:", 20)
        self.serial.sendall(b"live\n")
        self.read_until(b"$", 30)
        self.serial.sendall(b"stty -echo\n")
        time.sleep(0.2)
        return self.shell("id; uname -a; systemctl is-active lightdm; pgrep -a openbox")

    def shell(self, command, timeout=180):
        encoded = base64.b64encode(command.encode()).decode()
        marker = "VAULTBOOT_COMMAND_COMPLETE"
        self.buffer = b""
        line = f"printf %s {encoded} | base64 -d | bash; result=$?; printf '\\n{marker}:%s\\n' \"$result\"\n"
        self.serial.sendall(line.encode())
        result = self.read_until((marker + ":").encode(), timeout)
        code = self.read_until(b"\n", 5).strip()
        return {"output": result.rsplit(marker, 1)[0], "exit_code": int(code)}

    def close(self):
        if self.process.poll() is None:
            try:
                self.qmp("quit")
            except (OSError, ValueError, EOFError):
                self.process.terminate()
            self.process.wait(timeout=20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("work")
    parser.add_argument("--firmware", choices=["bios", "uefi"], default="bios")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    queue = work / "commands.jsonl"
    queue.touch(exist_ok=True)
    responses = work / "responses.jsonl"
    def emit(value):
        with responses.open("a") as stream:
            stream.write(json.dumps(value) + "\n")
    vm = VM(args.image, work, args.firmware, args.offline)
    try:
        try:
            emit({"event": "login", "result": vm.login()})
        except Exception as error:
            vm.qmp("screendump", {"filename": str(work / "login-failure.ppm")})
            emit({"event": "login", "error": str(error)})
        deadline = time.monotonic() + 1800
        with queue.open() as stream:
            while time.monotonic() < deadline and vm.process.poll() is None:
                line = stream.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                try:
                    command = json.loads(line)
                    kind = command["action"]
                    if kind == "shell":
                        result = vm.shell(command["command"], command.get("timeout", 180))
                    elif kind == "screenshot":
                        result = vm.qmp("screendump", {"filename": str(work / (command["name"] + ".ppm"))})
                    elif kind == "key":
                        result = vm.qmp("human-monitor-command", {"command-line": "sendkey " + command["key"]})
                    elif kind == "serial":
                        vm.serial.sendall(command["text"].encode())
                        result = "sent"
                    elif kind == "read":
                        result = vm.read_until(command["marker"].encode(), command.get("timeout", 30))
                    elif kind == "stop":
                        break
                    else:
                        raise ValueError("Unknown action")
                    emit({"id": command.get("id"), "result": result})
                except Exception as error:
                    emit({"id": command.get("id"), "error": str(error)})
    finally:
        vm.close()


if __name__ == "__main__":
    main()
