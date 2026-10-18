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
import uuid


class VM:
    login_password = "live"

    def __init__(self, image, work, firmware="bios", offline=False, memory=2048, share=None, data_disk=None, usb_disk=None):
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
                "-qmp", f"unix:{self.qmp_path},server=on,wait=off"]
        if share:
            directory = Path(share).resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("Share must be a directory")
            args += ["-virtfs", f"local,path={directory},mount_tag=vaultboot,security_model=none"]
        if data_disk:
            disk = Path(data_disk)
            if disk.is_symlink() or not disk.is_file() or disk.resolve() == image:
                raise ValueError("Data disk must be a regular file")
            args += ["-drive", f"file={disk.resolve()},format=raw,if=virtio"]
        if usb_disk:
            disk = Path(usb_disk)
            if (disk.is_symlink() or not disk.is_file() or disk.resolve() == image
                    or (data_disk and disk.resolve() == Path(data_disk).resolve())):
                raise ValueError("USB test disk must be a regular file")
            args += ["-device", "qemu-xhci,id=xhci",
                     "-drive", f"file={disk.resolve()},format=raw,if=none,id=flash",
                     "-device", "usb-storage,drive=flash,removable=on,serial=VAULTBOOT_TEST_USB"]
        args += ["-nic", "none"] if offline else ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0,id=nic0"]
        if firmware == "uefi":
            code = next(Path("/usr/share/OVMF").glob("OVMF_CODE_4M.fd"))
            variables = self.work / "OVMF_VARS.fd"
            if not variables.exists():
                shutil.copy2(code.with_name("OVMF_VARS_4M.fd"), variables)
            args += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                     "-drive", f"if=pflash,format=raw,file={variables}"]
        self.start_process(args)

    def start_process(self, args):
        self.process = subprocess.Popen(args, stdout=(self.work / "qemu.log").open("ab"), stderr=subprocess.STDOUT)
        self.serial = self.connect(self.serial_path)
        self.qmp_socket = self.connect(self.qmp_path)
        self.qmp_socket.settimeout(15)
        self.qmp_stream = self.qmp_socket.makefile("rwb", buffering=0)
        self.events = []
        self.qmp_id = 0
        if "QMP" not in self.qmp_read():
            raise RuntimeError("QEMU did not send a QMP greeting")
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

    def qmp_read(self):
        line = self.qmp_stream.readline()
        if not line:
            raise EOFError("QEMU monitor closed")
        return json.loads(line)

    def qmp(self, command, arguments=None):
        self.qmp_id += 1
        request_id = self.qmp_id
        self.qmp_stream.write(json.dumps({"execute": command, "arguments": arguments or {}, "id": request_id}).encode() + b"\n")
        deadline = time.monotonic() + 15
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("QEMU monitor timed out: " + command)
            self.qmp_socket.settimeout(max(0.1, deadline - time.monotonic()))
            result = self.qmp_read()
            if "event" in result:
                self.events.append(result)
                continue
            if result.get("id") != request_id:
                continue
            if "error" in result:
                raise RuntimeError(result)
            if "return" in result:
                self.qmp_socket.settimeout(15)
                return result["return"]

    def wait_event(self, event, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            for index, value in enumerate(self.events):
                if value.get("event") == event:
                    return self.events.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QEMU did not emit " + event)
            # The emulated UART can block guest shutdown if its socket fills.
            # Keep draining it while waiting for the monitor's reset event.
            # select also avoids poisoning SocketIO with an idle read timeout.
            ready, _, _ = select.select([self.qmp_socket, self.serial], [], [], min(1, remaining))
            if self.qmp_socket in ready:
                value = self.qmp_read()
                if value.get("event") == event:
                    return value
                if "event" in value:
                    self.events.append(value)
            if self.serial in ready:
                chunk = self.serial.recv(65536)
                if not chunk:
                    raise EOFError("Guest serial console closed while waiting for " + event)
                self.log.write(chunk)

    def read_until_any(self, markers, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            found = [(self.buffer.index(marker), marker) for marker in markers if marker in self.buffer]
            if found:
                offset, marker = min(found)
                end = offset + len(marker)
                result, self.buffer = self.buffer[:end], self.buffer[end:]
                return result.decode("utf-8", "replace"), marker
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Guest did not emit {markers!r}; tail: {self.buffer[-2000:]!r}")
            if not select.select([self.serial], [], [], 1)[0]:
                continue
            chunk = self.serial.recv(65536)
            if not chunk:
                raise EOFError("Guest serial console closed")
            self.log.write(chunk)
            self.buffer += chunk
            if len(self.buffer) > 4_000_000:
                raise RuntimeError("Serial output exceeded 4 MB without the expected marker; see serial.log")

    def read_until(self, marker, timeout=180):
        return self.read_until_any([marker], timeout)[0]

    def login(self, timeout=300):
        # Debian live-config only autologins tty1..6. ttyS0 normally asks for
        # writer/live, but accept an existing shell for manual recovery too.
        _, marker = self.read_until_any([b"login:", b"writer@vaultboot:"], timeout)
        if marker == b"login:":
            self.serial.sendall(b"writer\n")
            _, marker = self.read_until_any([b"Password:", b"writer@vaultboot:"], 20)
            if marker == b"Password:":
                self.serial.sendall((self.login_password + "\n").encode())
                _, marker = self.read_until_any([b"writer@vaultboot:", b"Login incorrect"], 30)
                if marker == b"Login incorrect":
                    raise RuntimeError(f"Guest rejected writer/{self.login_password}; check user-setup in the image")
        self.read_until(b"$ ", 30)
        self.serial.sendall(b"stty -echo\n")
        return self.shell("id && uname -a && systemctl is-active lightdm && pgrep -a openbox")

    def shell(self, command, timeout=180):
        encoded = base64.b64encode(command.encode()).decode()
        marker = "VAULTBOOT_COMPLETE_" + uuid.uuid4().hex
        self.buffer = b""
        payload = f"printf %s {encoded} | base64 -d | bash; result=$?; printf '\\n{marker}:%s\\n' \"$result\"\n"
        # Encode the wrapper too: an echoed input line must never contain the
        # completion marker. Chunk lines below the canonical tty input limit.
        payload = base64.b64encode(payload.encode()).decode()
        variable = "_vaultboot_" + uuid.uuid4().hex
        self.serial.sendall((variable + "=''\n").encode())
        for start in range(0, len(payload), 2000):
            self.serial.sendall(f'{variable}="${{{variable}}}{payload[start:start + 2000]}"\n'.encode())
        self.serial.sendall(f'printf %s "${{{variable}}}" | base64 -d | bash; unset {variable}\n'.encode())
        result = self.read_until((marker + ":").encode(), timeout)
        code = self.read_until(b"\n", 5).strip()
        return {"output": result.rsplit(marker, 1)[0], "exit_code": int(code)}

    def reboot(self, timeout=300):
        previous = self.shell("cat /proc/sys/kernel/random/boot_id")
        if previous["exit_code"]:
            raise RuntimeError("Could not read boot ID before reboot")
        self.qmp("query-status")
        self.events.clear()
        result = self.shell("sudo -n sh -c 'sync; (sleep 1; systemctl reboot) >/dev/null 2>&1 </dev/null &'", 30)
        if result["exit_code"]:
            raise RuntimeError("Guest reboot command failed: " + str(result))
        self.wait_event("RESET", timeout)
        self.buffer = b""
        while select.select([self.serial], [], [], 0)[0]:
            chunk = self.serial.recv(65536)
            if not chunk:
                raise EOFError("Serial console closed during reboot")
            self.log.write(chunk)
        login = self.login()
        current = self.shell("cat /proc/sys/kernel/random/boot_id")
        # Output can include a shell prompt, so compare the actual UUID strings.
        before = previous["output"].strip().splitlines()[-1]
        after = current["output"].strip().splitlines()[-1]
        if current["exit_code"] or before == after:
            raise RuntimeError("Guest did not acquire a new boot ID")
        return {"previous_boot_id": before, "boot_id": after, "login": login}

    def close(self):
        if self.process.poll() is None:
            try:
                self.qmp("quit")
            except (OSError, ValueError, EOFError, RuntimeError):
                self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.qmp_stream.close()
        self.qmp_socket.close()
        self.serial.close()
        self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("work")
    parser.add_argument("--firmware", choices=["bios", "uefi"], default="bios")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--memory", type=int, default=2048, choices=range(1024, 8193))
    parser.add_argument("--share", type=Path)
    parser.add_argument("--data-disk", type=Path)
    parser.add_argument("--usb-disk", type=Path)
    args = parser.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    vm = VM(args.image, work, args.firmware, args.offline, args.memory, args.share, args.data_disk, args.usb_disk)
    run_console(vm, work)


def run_console(vm, work, login_timeout=45, duration=1800):
    """Serve the same bounded JSONL command protocol for either architecture."""
    work = Path(work).resolve()
    queue = work / "commands.jsonl"
    queue.touch(exist_ok=True)
    responses = work / "responses.jsonl"
    def emit(value):
        with responses.open("a") as stream:
            stream.write(json.dumps(value) + "\n")
    try:
        try:
            emit({"event": "login", "result": vm.login(timeout=login_timeout)})
        except Exception as error:
            failure = {"event": "login", "error": str(error)}
            try:
                vm.qmp("screendump", {"filename": str(work / "login-failure.ppm")})
            except Exception as screenshot_error:
                failure["screenshot_error"] = str(screenshot_error)
            emit(failure)
        deadline = time.monotonic() + duration
        with queue.open() as stream:
            while time.monotonic() < deadline and vm.process.poll() is None:
                position = stream.tell()
                line = stream.readline()
                if not line.endswith("\n"):
                    stream.seek(position)
                    time.sleep(0.25)
                    continue
                command = {}
                try:
                    command = json.loads(line)
                    if not isinstance(command, dict):
                        command = {}
                        raise ValueError("A command must be a JSON object")
                    kind = command["action"]
                    if kind == "shell":
                        result = vm.shell(command["command"], command.get("timeout", 180))
                    elif kind == "screenshot":
                        result = vm.qmp("screendump", {"filename": str(work / (command["name"] + ".ppm"))})
                    elif kind == "key":
                        result = vm.qmp("human-monitor-command", {"command-line": "sendkey " + command["key"]})
                    elif kind == "login":
                        result = vm.login(command.get("timeout", 300))
                    elif kind == "reboot":
                        result = vm.reboot(command.get("timeout", 300))
                    elif kind == "network":
                        result = vm.qmp("set_link", {"name": "nic0", "up": bool(command["up"])})
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
