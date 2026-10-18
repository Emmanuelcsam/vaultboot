"""Exercise console framing using fake monitor/serial streams, without a VM."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import re
import socket
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("vm_console", Path(__file__).parent / "vm/console.py")
console = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(console)


class ConsoleTests(unittest.TestCase):
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
