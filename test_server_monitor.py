#!/usr/bin/env python3
"""
Unit tests for server_monitor.py

Regressions locked here were found by running the collector against a real
SSH host (homeserver) — canned fixtures alone did not surface them.
"""

import os
import tempfile
import unittest
from unittest import mock

from server_monitor import (
    SSH_COMMANDS,
    Panel3xUI,
    Report,
    Server,
    ServerInfo,
    build_xui_url,
    generate_text_report,
    parse_access_level,
    parse_bandwidth,
    parse_cpu_info,
    parse_cpu_usage,
    parse_disk,
    parse_input_file,
    parse_os,
    parse_public_ip,
    parse_ram,
    parse_server_type,
    parse_xui_result_env,
    parse_xui_settings,
    run_ssh_command,
    save_json,
    save_text_report,
)


class FakeChannel:
    def __init__(self, exit_code=0):
        self._exit_code = exit_code
        self._shutdown = False

    def recv_exit_status(self):
        return self._exit_code

    def shutdown_write(self):
        self._shutdown = True


class FakeStream:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class FakeStdin:
    def __init__(self):
        self.written = ""
        self.channel = FakeChannel()

    def write(self, text):
        self.written += text

    def flush(self):
        pass


class FakeClient:
    """Minimal paramiko.SSHClient stand-in.

    `responses` maps a substring of the command to (stdout, stderr, exit_code).
    """

    def __init__(self, responses=None, default=("", "", 0)):
        self.responses = responses or {}
        self.default = default
        self.commands = []
        self.stdin = FakeStdin()
        self.closed = False

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        for needle, response in self.responses.items():
            if needle in command:
                out, err, code = response
                stdout = FakeStream(out.encode())
                stderr = FakeStream(err.encode())
                stdout.channel = FakeChannel(code)
                return self.stdin, stdout, stderr
        out, err, code = self.default
        stdout = FakeStream(out.encode())
        stderr = FakeStream(err.encode())
        stdout.channel = FakeChannel(code)
        return self.stdin, stdout, stderr

    def close(self):
        self.closed = True


class StatefulXuiClient:
    """Fake client whose 3x-ui state flips from absent to present on install.

    Mirrors a real host: the panel only becomes detectable after the
    installer command has run.
    """

    def __init__(self, install_rc=0, install_out="", install_err=""):
        self.installed = False
        self.install_rc = install_rc
        self.install_out = install_out
        self.install_err = install_err
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)

        if "install.sh" in command and "curl" in command:
            if self.install_rc == 0:
                self.installed = True
            stdout = FakeStream(self.install_out.encode())
            stderr = FakeStream(self.install_err.encode())
            stdout.channel = FakeChannel(self.install_rc)
            return FakeStdin(), stdout, stderr

        if "PRESENT" in command and "ABSENT" in command:
            stdout = FakeStream(b"PRESENT\n" if self.installed else b"ABSENT\n")
            stdout.channel = FakeChannel(0)
            return FakeStdin(), stdout, FakeStream(b"")

        if "install-result.env" in command:
            if not self.installed:
                stdout = FakeStream(b"")
                stdout.channel = FakeChannel(1)
                return FakeStdin(), stdout, FakeStream(b"")
            body = (
                "XUI_USERNAME=paneluser\n"
                "XUI_PASSWORD=panelpass\n"
                "XUI_PANEL_PORT=2053\n"
                "XUI_WEB_BASE_PATH=abc123\n"
                "XUI_ACCESS_URL=http://1.2.3.4:2053/abc123\n"
            )
            stdout = FakeStream(body.encode())
            stdout.channel = FakeChannel(0)
            return FakeStdin(), stdout, FakeStream(b"")

        if "setting -show" in command:
            if not self.installed:
                stdout = FakeStream(b"")
                stdout.channel = FakeChannel(1)
                return FakeStdin(), stdout, FakeStream(b"")
            body = (
                "current panel settings as follows:\n"
                "hasDefaultCredential: false\n"
                "port: 2053\n"
                "webBasePath: abc123\n"
            )
            stdout = FakeStream(body.encode())
            stdout.channel = FakeChannel(0)
            return FakeStdin(), stdout, FakeStream(b"")

        if command.strip() == "id -u":
            stdout = FakeStream(b"0\n")
            stdout.channel = FakeChannel(0)
            return FakeStdin(), stdout, FakeStream(b"")

        stdout = FakeStream(b"")
        stdout.channel = FakeChannel(0)
        return FakeStdin(), stdout, FakeStream(b"")

    def close(self):
        pass


class TestServerMonitor(unittest.TestCase):

    def test_parse_input_file_valid(self):
        test_content = """
# Test servers
1.2.3.4:22@root:password123
5.6.7.8@user:password456
9.10.11.12:2222@admin:secret
        """.strip()
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(test_content)
            temp_path = f.name

        try:
            servers = parse_input_file(temp_path)
            self.assertEqual(len(servers), 3)

            self.assertEqual(servers[0].ip, "1.2.3.4")
            self.assertEqual(servers[0].port, 22)
            self.assertEqual(servers[0].username, "root")
            self.assertEqual(servers[0].password, "password123")

            # Default port 22
            self.assertEqual(servers[1].ip, "5.6.7.8")
            self.assertEqual(servers[1].port, 22)
            self.assertEqual(servers[1].username, "user")
            self.assertEqual(servers[1].password, "password456")

            self.assertEqual(servers[2].ip, "9.10.11.12")
            self.assertEqual(servers[2].port, 2222)
            self.assertEqual(servers[2].username, "admin")
            self.assertEqual(servers[2].password, "secret")
        finally:
            os.remove(temp_path)

    def test_parse_input_file_malformed(self):
        test_content = """
invalid_line_without_at
1.2.3.4:22@root:pass
        """.strip()
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write(test_content)
            temp_path = f.name

        try:
            servers = parse_input_file(temp_path)
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0].ip, "1.2.3.4")
        finally:
            os.remove(temp_path)

    def test_parse_os(self):
        output = 'NAME="Ubuntu"\nVERSION="22.04.3 LTS (Jammy Jellyfish)"'
        os_info = parse_os(output)
        self.assertEqual(os_info["name"], "Ubuntu")
        self.assertEqual(os_info["version"], "22.04.3 LTS (Jammy Jellyfish)")

    def test_parse_cpu_info(self):
        output = "Model name: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz\nCPU(s): 4"
        cpu_info = parse_cpu_info(output)
        self.assertEqual(cpu_info["model"], "Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz")
        self.assertEqual(cpu_info["cores"], 4)

    def test_parse_ram(self):
        output = "8192,2048,6144"
        ram_info = parse_ram(output)
        self.assertEqual(ram_info["total"], 8192)
        self.assertEqual(ram_info["used"], 2048)
        self.assertEqual(ram_info["free"], 6144)

    def test_parse_server_type(self):
        self.assertEqual(parse_server_type("kvm\nVirtualBox"), "VM")
        self.assertEqual(parse_server_type("docker\nunknown"), "container")
        self.assertEqual(parse_server_type("none\nDell Inc."), "bare_metal")

    def test_parse_server_type_unknown_probe(self):
        # Probe unavailable -> "unknown", not a guessed bare_metal.
        self.assertEqual(parse_server_type("unknown\nunknown"), "unknown")
        self.assertEqual(parse_server_type(""), "unknown")

    def test_parse_access_level(self):
        self.assertEqual(parse_access_level("root"), "root")
        self.assertEqual(parse_access_level("ubuntu"), "user")

    def test_parse_cpu_usage(self):
        self.assertEqual(parse_cpu_usage("4.5\n"), 4.5)
        self.assertEqual(parse_cpu_usage(""), 0.0)
        self.assertEqual(parse_cpu_usage("not-a-number"), 0.0)

    def test_parse_public_ip_rejects_html(self):
        """ifconfig.me returned an HTML 403 page; that must not become the IP."""
        html = (
            "\n<html><head>\n<meta http-equiv=\"content-type\" "
            "content=\"text/html;charset=utf-8\">\n<title>403 Forbidden</title>\n"
            "</head>\n<body text=#000000 bgcolor=#ffffff>\n"
            "<h1>Error: Forbidden</h1>\n</body></html>\n"
        )
        self.assertEqual(parse_public_ip(html), "unknown")

    def test_parse_public_ip_picks_bare_address(self):
        self.assertEqual(parse_public_ip("\n37.148.73.77\n94.20.158.201\n"), "37.148.73.77")
        self.assertEqual(parse_public_ip("1.1.1.1\n"), "1.1.1.1")

    def test_parse_bandwidth(self):
        """Real output comes from /sys/class/net/*/speed as 'name speed' pairs."""
        output = "br-51f1426067c4 10000\ndocker0 NA\nenp3s0 NA\nlo NA\n"
        parsed = parse_bandwidth(output)
        names = [i["name"] for i in parsed["interfaces"]]
        speeds = [i["speed"] for i in parsed["interfaces"]]
        self.assertEqual(names, ["br-51f1426067c4", "docker0", "enp3s0", "lo"])
        self.assertEqual(speeds, ["10000", "NA", "NA", "NA"])

    def test_parse_disk_root_only(self):
        """The report needs total + used for "/" only — not every mount."""
        output = (
            "/dev/sda6 41G 13G 26G 34% /\n"
            "tmpfs 3.9G 0 3.9G 0% /dev/shm\n"
            "/dev/sda1 511M 130M 381M 26% /boot\n"
        )
        disks = parse_disk(output)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/")
        self.assertEqual(disks[0]["total"], 41.0)
        self.assertEqual(disks[0]["used"], 13.0)

    def test_parse_disk_falls_back_to_largest(self):
        """No "/" in the output (unusual) — take the largest real filesystem."""
        output = "tmpfs 3.9G 0 3.9G 0% /dev/shm\n/dev/sdb1 200G 10G 190G 5% /data\n"
        disks = parse_disk(output)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/data")

    def test_ssh_commands_avoid_shell_expansion(self):
        """awk programs must stay single-quoted.

        A double-quoted awk program lets the remote shell expand $1 first,
        which made the cpu_usage probe fail with an awk syntax error.
        """
        for name, cmd in SSH_COMMANDS.items():
            self.assertNotIn('awk "', cmd, f"{name} uses a double-quoted awk program")
            self.assertNotIn('sed "', cmd, f"{name} uses a double-quoted sed program")

    def test_ssh_commands_cpu_usage_uses_proc_stat_delta(self):
        """top -bn1 reports the since-boot average (~0% on an idle host).

        The probe must sample /proc/stat twice so the figure is real.
        """
        cmd = SSH_COMMANDS["cpu_usage"]
        self.assertIn("/proc/stat", cmd)
        self.assertIn("sleep 1", cmd)
        self.assertNotIn("top -bn1", cmd)

    def test_ssh_commands_cpu_usage_awk_is_single_quoted(self):
        """The final awk program must survive the remote shell unexpanded."""
        cmd = SSH_COMMANDS["cpu_usage"]
        self.assertIn("'BEGIN{", cmd)
        self.assertNotIn('"BEGIN{', cmd)

    def test_ssh_commands_disk_has_posix_fallback(self):
        """busybox/Alpine df has no --output flag."""
        self.assertIn("df -P -h", SSH_COMMANDS["disk"])

    def test_ssh_commands_bandwidth_uses_sysfs(self):
        """'ip link' carries no speed; only /sys/class/net/*/speed does."""
        self.assertIn("/sys/class/net/", SSH_COMMANDS["bandwidth"])


class TestProbeVerdict(unittest.TestCase):
    """Regression: stderr alone must not mark a probe as failed.

    35 of 36 servers were reported as "partial" because a tool warning on
    stderr was treated as an error while the exit status was never checked.
    """

    def test_run_ssh_command_returns_exit_code(self):
        client = FakeClient({"nosuchcmd": ("", "nosuchcmd: not found", 127)})
        out, err, code = run_ssh_command(client, "nosuchcmd")
        self.assertEqual(out, "")
        self.assertEqual(code, 127)
        self.assertIn("not found", err)

    def test_run_ssh_command_success_with_stderr_warning(self):
        """A warning on stderr with exit status 0 is a success."""
        client = FakeClient({"df": ("/dev/sda1 41G 13G 26G 34% /\n", "df: warning\n", 0)})
        out, err, code = run_ssh_command(client, "df -P -h")
        self.assertEqual(code, 0)
        self.assertIn("/dev/sda1", out)
        self.assertIn("warning", err)


class Test3xUI(unittest.TestCase):

    def test_parse_xui_settings(self):
        output = (
            "current panel settings as follows:\n"
            "hasDefaultCredential: false\n"
            "port: 2053\n"
            "webBasePath: abcdef123456\n"
        )
        settings = parse_xui_settings(output)
        self.assertEqual(settings["port"], 2053)
        self.assertEqual(settings["web_base_path"], "abcdef123456")
        self.assertIs(settings["has_default_credential"], False)

    def test_parse_xui_result_env(self):
        output = (
            "XUI_USERNAME=paneluser\n"
            "XUI_PASSWORD=s3cret\n"
            "XUI_PANEL_PORT=2053\n"
            "XUI_WEB_BASE_PATH=abc123\n"
            "XUI_ACCESS_URL=http://1.2.3.4:2053/abc123\n"
        )
        state = parse_xui_result_env(output)
        self.assertEqual(state["username"], "paneluser")
        self.assertEqual(state["password"], "s3cret")
        self.assertEqual(state["port"], 2053)
        self.assertEqual(state["web_base_path"], "abc123")
        self.assertEqual(state["url"], "http://1.2.3.4:2053/abc123")

    def test_build_xui_url(self):
        self.assertEqual(
            build_xui_url("1.2.3.4", 2053, "abc"),
            "http://1.2.3.4:2053/abc",
        )
        self.assertIsNone(build_xui_url("1.2.3.4", None, "abc"))

    def test_detect_xui_absent(self):
        client = FakeClient({"PRESENT": ("ABSENT\n", "", 0)})
        from server_monitor import detect_xui
        self.assertFalse(detect_xui(client))

    def test_detect_xui_present(self):
        client = FakeClient({"PRESENT": ("PRESENT\n", "", 0)})
        from server_monitor import detect_xui
        self.assertTrue(detect_xui(client))


def _non_root_exec(client):
    """Wrap exec_command so `id -u` reports a non-root uid."""
    original = client.exec_command

    def exec_command(command, timeout=None):
        if command.strip() == "id -u":
            stdout = FakeStream(b"1000\n")
            stdout.channel = FakeChannel(0)
            return FakeStdin(), stdout, FakeStream(b"")
        return original(command, timeout=timeout)

    return exec_command


class TestXuiFlow(unittest.TestCase):
    """End-to-end 3x-ui flow: detect -> install -> verify -> read credentials."""

    def _server(self, user="root", password="secret"):
        return Server(ip="1.2.3.4", port=22, username=user, password=password)

    def test_install_then_credentials_are_read(self):
        from server_monitor import collect_xui
        client = StatefulXuiClient(install_out="x-ui installation finished")
        panel = collect_xui(self._server(), client, timeout=5, install_timeout=60)
        self.assertEqual(panel.state, "installed")
        self.assertEqual(panel.port, 2053)
        self.assertEqual(panel.web_base_path, "abc123")
        self.assertEqual(panel.username, "paneluser")
        self.assertEqual(panel.password, "panelpass")
        self.assertEqual(panel.url, "http://1.2.3.4:2053/abc123")

    def test_preexisting_panel_is_not_reinstalled(self):
        from server_monitor import collect_xui
        client = StatefulXuiClient()
        client.installed = True
        panel = collect_xui(self._server(), client, timeout=5, install_timeout=60)
        self.assertEqual(panel.state, "preexisting")
        self.assertFalse(any("install.sh" in c for c in client.commands))
        self.assertEqual(panel.username, "paneluser")

    def test_install_failure_is_classified(self):
        from server_monitor import collect_xui
        client = StatefulXuiClient(install_rc=1, install_err="Fatal error: Please run this script with root privilege")
        panel = collect_xui(self._server(user="deploy"), client, timeout=5, install_timeout=60)
        self.assertEqual(panel.state, "not_attempted")
        self.assertIn("root required", panel.reason)

    def test_install_failure_without_root_cause_is_failed(self):
        from server_monitor import collect_xui
        client = StatefulXuiClient(install_rc=1, install_err="curl: (6) Could not resolve host")
        panel = collect_xui(self._server(), client, timeout=5, install_timeout=60)
        self.assertEqual(panel.state, "failed")
        self.assertIn("DNS", panel.reason)

    def test_install_command_uses_noninteractive_and_no_bashism(self):
        from server_monitor import install_xui
        client = StatefulXuiClient(install_out="ok")
        install_xui(client, "secret", timeout=60)
        cmd = [c for c in client.commands if "install.sh" in c][0]
        self.assertIn("XUI_NONINTERACTIVE=1", cmd)
        # `bash <(...)` is a bashism; process substitution needs bash.
        self.assertNotIn("bash <(", cmd)
        self.assertIn("| bash", cmd)

    def test_non_root_install_elevates_with_sudo(self):
        from server_monitor import install_xui
        client = StatefulXuiClient(install_out="ok")
        client.exec_command = _non_root_exec(client)
        ok, _tail, mode = install_xui(client, "secret", timeout=60)
        self.assertEqual(mode, "sudo")
        cmd = [c for c in client.commands if "install.sh" in c][0]
        self.assertIn("sudo -S -p ''", cmd)
        self.assertIn("env XUI_NONINTERACTIVE=1", cmd)

    def test_sudo_password_is_not_in_the_command_string(self):
        from server_monitor import install_xui
        client = StatefulXuiClient(install_out="ok")
        install_xui(client, "sup3rs3cret", timeout=60)
        cmd = [c for c in client.commands if "install.sh" in c][0]
        self.assertNotIn("sup3rs3cret", cmd)


class TestTextReport(unittest.TestCase):

    def _report(self):
        server = Server(ip="1.2.3.4", port=22, username="root", password="secret")
        installed = ServerInfo(
            server=server,
            success=True,
            os={"name": "Ubuntu", "version": "22.04"},
            cpu={"model": "Xeon", "usage": 2.0},
            ram={"total": 8192, "used": 2048, "free": 6144},
            disk=[{"mount": "/", "total": 41.0, "used": 13.0, "free": 26.0, "unit": "GB"}],
            server_type="bare_metal",
            access_level="root",
            public_ip="1.2.3.4",
            bandwidth={"interfaces": [{"name": "eth0", "speed": "1000"}]},
            ports=[22, 2053],
            top_processes=[],
            partial_errors=[],
            x3ui=Panel3xUI(
                state="installed",
                url="http://1.2.3.4:2053/abc",
                port=2053,
                username="paneluser",
                password="panelpass",
            ),
        )
        other = Server(ip="5.6.7.8", port=22, username="user", password="pw")
        failed = ServerInfo(
            server=other,
            success=True,
            os={"name": "Debian", "version": "12"},
            x3ui=Panel3xUI(state="not_attempted", reason="root required"),
        )
        return Report(
            timestamp="2026-10-20T00:00:00+00:00",
            input_file="servers.txt",
            servers=[installed, failed],
            summary={"total": 2, "success": 1, "failed": 1, "reachable": 2, "partial": 0},
            x3ui_summary={"installed": 1, "preexisting": 0, "failed": 0, "not_attempted": 1},
        )

    def test_report_has_no_markdown_table(self):
        text = generate_text_report(self._report())
        self.assertNotIn("|", text)
        self.assertNotIn("--- |", text)

    def test_report_lists_disk_total_and_used(self):
        text = generate_text_report(self._report())
        self.assertIn("Disk:", text)
        self.assertIn("41", text)
        self.assertIn("13", text)

    def test_report_has_3xui_category(self):
        text = generate_text_report(self._report())
        self.assertIn("INSTALLED BY THIS RUN (1)", text)
        self.assertIn("NOT INSTALLED (1)", text)
        self.assertIn("1.2.3.4:22", text)

    def test_report_has_panel_credentials(self):
        text = generate_text_report(self._report())
        self.assertIn("http://1.2.3.4:2053/abc", text)
        self.assertIn("paneluser", text)
        self.assertIn("panelpass", text)

    def test_save_text_report_writes_file(self):
        report = self._report()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "report.txt")
            save_text_report(report, path)
            with open(path) as f:
                content = f.read()
            self.assertIn("3x-ui PANEL ACCESS", content)
            self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")

    def test_save_json_strips_ssh_password(self):
        report = self._report()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "report.json")
            save_json(report, path)
            with open(path) as f:
                content = f.read()
            self.assertNotIn("secret", content)
            self.assertIn("panelpass", content)
            self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")


if __name__ == "__main__":
    unittest.main()
