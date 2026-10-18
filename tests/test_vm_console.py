"""Exercise console framing using fake monitor/serial streams, without a VM."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import re
import socket
import threading
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("vm_console", Path(__file__).parent / "vm/console.py")
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


class ConsoleTests(unittest.TestCase):
    def test_usb_write_target_cannot_be_source_or_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.img"
            source.write_bytes(b"source must remain intact")
            link = root / "link.img"
            link.symlink_to(source)
            with patch.object(console.subprocess, "run"), patch.object(console.VM, "start_process") as start:
                for target in (source, link):
                    with self.subTest(target=target), self.assertRaises(ValueError):
                        console.VM(source, root / "vm", usb_disk=target)
                start.assert_not_called()
            self.assertEqual(source.read_bytes(), b"source must remain intact")

    def test_usb_test_target_appears_as_removable_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source.img", root / "usb.img"
            source.touch()
            target.touch()
            with patch.object(console.subprocess, "run"), patch.object(console.VM, "start_process") as start:
                console.VM(source, root / "vm", usb_disk=target)
            args = start.call_args.args[0]
            self.assertIn("usb-storage,drive=flash,removable=on,serial=VAULTBOOT_TEST_USB", args)
            self.assertIn(f"file={target},format=raw,if=none,id=flash", args)

    def vm(self):
        vm = console.VM.__new__(console.VM)
        vm.buffer = b""
        vm.log = io.BytesIO()
        vm.serial = MagicMock()
        vm.qmp_socket = MagicMock()
        vm.qmp_stream = MagicMock()
        vm.events = []
        vm.qmp_id = 0
        return vm

    def test_read_until_keeps_trailing_bytes_for_next_read(self):
        vm = self.vm()
        vm.buffer = b"boot text\r\nlogin: following"
        self.assertEqual(vm.read_until(b"login:"), "boot text\r\nlogin:")
        self.assertEqual(vm.buffer, b" following")

    def test_read_until_handles_real_socket_data_and_eof(self):
        vm = self.vm()
        local, peer = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(peer.close)
        vm.serial = local
        peer.sendall(b"some output\nDONE\n")
        self.assertEqual(vm.read_until(b"DONE", 1), "some output\nDONE")
        self.assertEqual(vm.log.getvalue(), b"some output\nDONE\n")
        peer.close()
        with self.assertRaises(EOFError):
            vm.read_until(b"missing", 1)

    def test_qmp_skips_events_and_unrelated_responses(self):
        vm = self.vm()
        vm.qmp_stream.readline.side_effect = [b'{"event":"RESET"}\n', b'{"return":{},"id":99}\n', b'{"return":{"status":"running"},"id":1}\n']
        self.assertEqual(vm.qmp("query-status"), {"status": "running"})
        self.assertEqual(vm.events, [{"event": "RESET"}])
        sent = json.loads(vm.qmp_stream.write.call_args.args[0])
        self.assertEqual(sent["id"], 1)

    def test_qmp_eof_is_a_useful_error(self):
        vm = self.vm()
        vm.qmp_stream.readline.return_value = b""
        with self.assertRaisesRegex(EOFError, "monitor closed"):
            vm.qmp("query-status")

    def monitor_vm(self):
        vm = self.vm()
        vm.qmp_socket, qmp_peer = socket.socketpair()
        vm.serial, serial_peer = socket.socketpair()
        for connection in (vm.qmp_socket, qmp_peer, vm.serial, serial_peer):
            self.addCleanup(connection.close)
        vm.qmp_stream = vm.qmp_socket.makefile("rwb", buffering=0)
        self.addCleanup(vm.qmp_stream.close)
        return vm, qmp_peer, serial_peer

    def test_reset_wait_drains_serial_so_guest_can_finish_shutdown(self):
        vm, qmp_peer, serial_peer = self.monitor_vm()
        serial_peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        serial_peer.settimeout(2)
        payload = b"shutdown progress\n" * 16384
        failures = []

        def guest():
            try:
                # RESET cannot happen until the UART output has been consumed.
                serial_peer.sendall(payload)
                qmp_peer.sendall(b'{"event":"RESET"}\n')
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=guest, daemon=True)
        worker.start()
        try:
            self.assertEqual(vm.wait_event("RESET", 3), {"event": "RESET"})
        finally:
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(vm.log.getvalue())
        self.assertTrue(payload.startswith(vm.log.getvalue()))

    def test_event_timeout_leaves_monitor_usable_for_diagnostics(self):
        vm, qmp_peer, _ = self.monitor_vm()
        with self.assertRaisesRegex(TimeoutError, "did not emit RESET"):
            vm.wait_event("RESET", 0.02)
        qmp_peer.sendall(b'{"event":"RESET"}\n')
        self.assertEqual(vm.wait_event("RESET", 1), {"event": "RESET"})

    def test_shell_marker_is_never_present_in_echoed_input_and_lines_are_bounded(self):
        vm = self.vm()
        command = "printf '%s' '" + "a" * 20_000 + "'; exit 17"
        markers = []

        def read(marker, timeout):
            if marker == b"\n":
                return "17\r\n"
            markers.append(marker)
            self.assertNotIn(marker, b"".join(call.args[0] for call in vm.serial.sendall.call_args_list))
            return "expected command output\n" + marker.decode()

        vm.read_until = MagicMock(side_effect=read)
        answer = vm.shell(command)
        self.assertEqual(answer, {"output": "expected command output\n", "exit_code": 17})
        lines = [call.args[0].decode() for call in vm.serial.sendall.call_args_list]
        self.assertLess(max(map(len, lines)), 2200)
        chunks = [re.search(r'\}([A-Za-z0-9+/=]+)"\n$', line).group(1) for line in lines[1:-1]]
        wrapper = base64.b64decode("".join(chunks)).decode()
        original = base64.b64decode(re.search(r"printf %s ([A-Za-z0-9+/=]+) \|", wrapper).group(1)).decode()
        self.assertEqual(original, command)
        self.assertIn(markers[0].decode(), wrapper)

    def test_login_handles_serial_password_prompt(self):
        vm = self.vm()
        vm.read_until_any = MagicMock(side_effect=[("login:", b"login:"), ("Password:", b"Password:"), ("writer@vaultboot:", b"writer@vaultboot:")])
        vm.read_until = MagicMock(return_value="~$ ")
        vm.shell = MagicMock(return_value={"exit_code": 0})
        self.assertEqual(vm.login(), {"exit_code": 0})
        self.assertEqual([call.args[0] for call in vm.serial.sendall.call_args_list], [b"writer\n", b"live\n", b"stty -echo\n"])

    def test_login_rejects_bad_credentials_immediately(self):
        vm = self.vm()
        vm.read_until_any = MagicMock(side_effect=[("login:", b"login:"), ("Password:", b"Password:"), ("Login incorrect", b"Login incorrect")])
        with self.assertRaisesRegex(RuntimeError, "writer/live"):
            vm.login()

    def test_login_accepts_existing_autologin_shell(self):
        vm = self.vm()
        vm.read_until_any = MagicMock(return_value=("writer@vaultboot:", b"writer@vaultboot:"))
        vm.read_until = MagicMock(return_value="~$ ")
        vm.shell = MagicMock(return_value={"exit_code": 0})
        vm.login()
        vm.serial.sendall.assert_called_once_with(b"stty -echo\n")

    def test_reboot_waits_for_reset_and_checks_new_boot_id(self):
        vm = self.vm()
        vm.shell = MagicMock(side_effect=[{"output": "old-id\n", "exit_code": 0}, {"output": "", "exit_code": 0}, {"output": "new-id\n", "exit_code": 0}])
        vm.qmp = MagicMock()
        vm.wait_event = MagicMock()
        vm.login = MagicMock(return_value={"exit_code": 0})
        with patch.object(console.select, "select", return_value=([], [], [])):
            result = vm.reboot()
        vm.wait_event.assert_called_once_with("RESET", 300)
        self.assertEqual(result["boot_id"], "new-id")
        self.assertEqual(result["previous_boot_id"], "old-id")


if __name__ == "__main__":
    unittest.main()
