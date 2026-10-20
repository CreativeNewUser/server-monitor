#!/usr/bin/env python3
"""
Server Monitor - Parallel SSH
Collects system information from multiple servers via SSH in parallel batches.
"""

import argparse
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

try:
    import paramiko
except ImportError:
    paramiko = None

# --- Self-provisioning: install paramiko + re-exec ---
# Order matters: distro package first (fast, no PEP 668 issues), pip last.

def _run(cmd: List[str], timeout: int = 120) -> bool:
    """Run a command, return True on exit code 0."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if res.returncode != 0:
            print(f"    ! {' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[:300]}", file=sys.stderr)
        return res.returncode == 0
    except Exception as e:
        print(f"    ! {' '.join(cmd[:3])} error: {e}", file=sys.stderr)
        return False


def _install_paramiko() -> bool:
    """Install paramiko using whatever package manager this host has."""
    def _importable() -> bool:
        try:
            importlib.import_module("paramiko")
            return True
        except ImportError:
            return False

    if _importable():
        return True

    strategies: List[List[List[str]]] = []

    if shutil.which("apt-get"):
        strategies.append([
            ["apt-get", "update", "-qq"],
            ["apt-get", "install", "-y", "-qq", "python3-paramiko"],
        ])
    if shutil.which("apk"):
        strategies.append([["apk", "add", "--no-cache", "py3-paramiko"]])
    if shutil.which("dnf"):
        strategies.append([["dnf", "install", "-y", "-q", "python3-paramiko"]])
    if shutil.which("yum"):
        strategies.append([["yum", "install", "-y", "-q", "python3-paramiko"]])

    pip_cmds = []
    for pip in ("pip3", "pip"):
        if shutil.which(pip):
            pip_cmds.append([pip, "install", "paramiko"])
            pip_cmds.append([pip, "install", "--break-system-packages", "paramiko"])
            pip_cmds.append([pip, "install", "--user", "paramiko"])
            break
    for extra in ([], ["--break-system-packages"], ["--user"]):
        pip_cmds.append([sys.executable, "-m", "pip", "install"] + extra + ["paramiko"])
    if shutil.which("uv"):
        pip_cmds.append(["uv", "pip", "install", "--system", "--break-system-packages", "paramiko"])
    strategies.append(pip_cmds)

    for group in strategies:
        for cmd in group:
            if _run(cmd) and _importable():
                return True

    # Last resort: private venv (works without root), then import from it.
    venv_dir = os.path.join(tempfile.gettempdir(), "server_monitor_venv")
    if _run([sys.executable, "-m", "venv", venv_dir], timeout=180):
        vpy = os.path.join(venv_dir, "bin", "python3")
        if not os.path.exists(vpy):
            vpy = os.path.join(venv_dir, "Scripts", "python.exe")
        vpip = os.path.join(venv_dir, "bin", "pip")
        if os.path.exists(vpip):
            if _run([vpip, "install", "paramiko"], timeout=300):
                _venv_site_packages(venv_dir)
                return _importable()
        # ensurepip missing -> bootstrap pip with get-pip.py (stdlib only)
        try:
            import urllib.request
            getpip = os.path.join(venv_dir, "get-pip.py")
            urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", getpip)
            if _run([vpy, getpip], timeout=300):
                if _run([vpy, "-m", "pip", "install", "paramiko"], timeout=300):
                    _venv_site_packages(venv_dir)
                    return _importable()
        except Exception as e:
            print(f"    ! venv bootstrap error: {e}", file=sys.stderr)

    return _importable()


def _venv_site_packages(venv_dir: str) -> None:
    """Make a venv's site-packages importable from the running interpreter."""
    import site
    for lib in ("lib", "lib64"):
        base = os.path.join(venv_dir, lib)
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            sp = os.path.join(base, entry, "site-packages")
            if os.path.isdir(sp) and sp not in sys.path:
                sys.path.append(sp)
                site.addsitedir(sp)


def _bootstrap() -> None:
    """Ensure paramiko is importable; install it if needed."""
    global paramiko
    if paramiko is not None:
        return
    print("[*] 'paramiko' not found -> auto-installing dependencies...", file=sys.stderr)
    if not _install_paramiko():
        print("[-] Auto-install failed. Install manually:\n"
              "      apt install -y python3-paramiko   (Debian/Ubuntu)\n"
              "      apk add py3-paramiko              (Alpine)\n"
              "      pip install paramiko              (pip)", file=sys.stderr)
        sys.exit(1)
    paramiko = importlib.import_module("paramiko")
    print("[+] 'paramiko' ready.", file=sys.stderr)

# --- Data Models ---

@dataclass
class Server:
    """Represents a server from the input file."""
    ip: str
    port: int = 22
    username: str = ""
    password: str = ""
    raw_line: str = ""


@dataclass
class ServerInfo:
    """Structured data collected from a server."""
    server: Server
    success: bool = False
    error: Optional[str] = None
    os: Optional[Dict[str, str]] = None
    cpu: Optional[Dict[str, Any]] = None
    ram: Optional[Dict[str, int]] = None
    disk: Optional[List[Dict[str, Any]]] = None
    server_type: Optional[str] = None
    access_level: Optional[str] = None
    public_ip: Optional[str] = None
    bandwidth: Optional[Dict[str, Any]] = None
    ports: Optional[List[int]] = None
    top_processes: Optional[List[Dict[str, Any]]] = None
    partial_errors: List[str] = field(default_factory=list)


@dataclass
class Report:
    """Aggregated results for all servers."""
    timestamp: str
    input_file: str
    servers: List[ServerInfo] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)


# --- SSH Commands ---
# Commands defined in contracts/ssh-commands.md

SSH_COMMANDS = {
    "os": "cat /etc/os-release 2>/dev/null || (cat /etc/redhat-release 2>/dev/null && cat /etc/issue 2>/dev/null) || uname -srm",
    "cpu_info": "lscpu 2>/dev/null || cat /proc/cpuinfo | head -20",
    # CPU usage from a /proc/stat delta.
    # `top -bn1` reports the since-boot average (~0% on an idle host) and is
    # locale-dependent, so it cannot be trusted. Two samples 1s apart give a
    # real instantaneous figure. Uses POSIX sh only (no bashisms).
    "cpu_usage": r"""t0=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); sleep 1; t1=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat); set -- $t0 $t1; awk -v t0=$1 -v i0=$2 -v t1=$3 -v i1=$4 'BEGIN{dt=t1-t0; di=i1-i0; if(dt>0) printf "%.1f\n", 100-100*di/dt}'""",
    "ram": "free -m 2>/dev/null | awk 'NR==2{printf \"%s,%s,%s\", $2,$3,$4}'",
    # GNU df --output first, POSIX df -P as fallback (busybox/Alpine).
    "disk": "(df -h --output=source,size,used,avail,pcent,target 2>/dev/null || df -P -h 2>/dev/null) | tail -n +2 | tr -s ' '",
    "server_type": "(systemd-detect-virt 2>/dev/null || echo 'unknown') && (dmidecode -s system-product-name 2>/dev/null || echo 'unknown')",
    "access_level": "whoami",
    "public_ip": "curl -s --max-time 5 ifconfig.me 2>/dev/null | head -1; curl -s --max-time 5 icanhazip.com 2>/dev/null | head -1; curl -s --max-time 5 ipinfo.io/ip 2>/dev/null | head -1",
    # Interface speed lives in sysfs, not in `ip link` output. -1 means unknown.
    "bandwidth": "for d in /sys/class/net/*; do n=$(basename $d); s=$(cat $d/speed 2>/dev/null); [ -z \"$s\" ] && s=NA; [ \"$s\" = \"-1\" ] && s=NA; echo \"$n $s\"; done",
    "ports": "ss -tulnH 2>/dev/null | awk '{print $5}' | awk -F: '{print $NF}' | sort -un",
    "top_processes": "ps aux --sort=-%cpu,-%mem | head -6 | tail -5 | awk '{printf \"%s,%s,%s,%s\\n\", $2,$11,$3,$4}'",
}


# --- Parsing ---

def parse_os(output: str) -> Dict[str, str]:
    """Parse OS info from command output."""
    result = {"name": "unknown", "version": "unknown", "kernel": "unknown"}
    for line in output.splitlines():
        if line.startswith("NAME="):
            result["name"] = line.split("=", 1)[1].strip('"').strip("'")
        elif line.startswith("VERSION="):
            result["version"] = line.split("=", 1)[1].strip('"').strip("'")
    if result["name"] == "unknown":
        if "redhat" in output.lower() or "centos" in output.lower():
            for line in output.splitlines():
                if "release" in line.lower():
                    result["name"] = line.split()[0]
                    result["version"] = " ".join(line.split()[1:3])
        else:
            # Fallback to uname
            parts = output.strip().split()
            if len(parts) >= 3:
                result["kernel"] = parts[2]
                result["name"] = parts[0]
    return result


def parse_cpu_info(output: str) -> Dict[str, Any]:
    """Parse CPU info from lscpu output."""
    result = {"model": "unknown", "cores": 0}
    for line in output.splitlines():
        if "Model name:" in line:
            result["model"] = line.split(":", 1)[1].strip()
        elif "CPU(s):" in line and "On-line" not in line:
            try:
                result["cores"] = int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return result


def parse_cpu_usage(output: str) -> float:
    """Parse CPU usage percentage."""
    try:
        return float(output.strip())
    except ValueError:
        return 0.0


def parse_ram(output: str) -> Dict[str, int]:
    """Parse RAM info (total, used, free in MB)."""
    parts = output.strip().split(",")
    if len(parts) >= 3:
        return {
            "total": int(parts[0]),
            "used": int(parts[1]),
            "free": int(parts[2]),
        }
    return {"total": 0, "used": 0, "free": 0}


def parse_disk(output: str) -> List[Dict[str, Any]]:
    """Parse disk info from df output."""
    disks = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 6:
            try:
                size = float(parts[1].replace("G", "").replace("M", ""))
                used = float(parts[2].replace("G", "").replace("M", ""))
                avail = float(parts[3].replace("G", "").replace("M", ""))
                if parts[1].endswith("M"):
                    size /= 1024
                    used /= 1024
                    avail /= 1024
                disks.append({
                    "mount": parts[5],
                    "total": round(size, 2),
                    "used": round(used, 2),
                    "free": round(avail, 2),
                    "unit": "GB",
                })
            except (ValueError, IndexError):
                continue
    return disks


def parse_server_type(output: str) -> str:
    """Parse server type from systemd-detect-virt and dmidecode.

    systemd-detect-virt prints 'none' on bare metal, and a virt name on VMs.
    'unknown' means the probe itself is unavailable.
    """
    lines = output.strip().splitlines()
    if not lines:
        return "unknown"
    virt = lines[0].strip().lower()
    if virt in ("", "unknown"):
        # Probe missing: fall back to container markers, else assume bare metal.
        if "docker" in output.lower() or "lxc" in output.lower():
            return "container"
        return "unknown"
    if virt == "none":
        return "bare_metal"
    if virt in ("docker", "lxc", "podman", "containerd"):
        return "container"
    if virt in ("kvm", "qemu", "vmware", "virtualbox", "xen", "hyperv", "virtualmachine"):
        return "VM"
    if "virtual" in virt:
        return "VM"
    return "bare_metal"


def parse_access_level(output: str) -> str:
    """Parse access level from whoami."""
    return "root" if output.strip() == "root" else "user"


def parse_public_ip(output: str) -> str:
    """Parse public IP: keep the first line that is a bare IPv4/IPv6 address."""
    for line in output.splitlines():
        candidate = line.strip()
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", candidate) and not candidate.startswith("."):
            return candidate
    return "unknown"


def parse_bandwidth(output: str) -> Dict[str, Any]:
    """Parse bandwidth from `name speed` lines (sysfs /sys/class/net/*/speed)."""
    interfaces = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            interfaces.append({
                "name": parts[0],
                "speed": parts[1],
            })
    return {"interfaces": interfaces}


def parse_ports(output: str) -> List[int]:
    """Parse open ports."""
    ports = []
    for line in output.strip().splitlines():
        try:
            ports.append(int(line.strip()))
        except ValueError:
            continue
    return ports


def parse_top_processes(output: str) -> List[Dict[str, Any]]:
    """Parse top 5 processes."""
    processes = []
    for line in output.strip().splitlines():
        parts = line.split(",")
        if len(parts) >= 4:
            try:
                processes.append({
                    "pid": int(parts[0]),
                    "name": parts[1],
                    "cpu_percent": float(parts[2]),
                    "ram_percent": float(parts[3]),
                })
            except (ValueError, IndexError):
                continue
    return processes


# --- SSH Connection ---

def ssh_connect(server: Server, timeout: int = 15) -> Optional[Any]:
    """Establish SSH connection to a server."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=server.ip,
            port=server.port,
            username=server.username,
            password=server.password,
            timeout=timeout,
            banner_timeout=5,
            auth_timeout=5,
        )
        return client
    except Exception as e:
        logging.error(f"SSH connection failed for {server.ip}:{server.port}@{server.username}: {e}")
        return None


def run_ssh_command(client: Any, command: str, timeout: int = 15) -> Tuple[str, Optional[str]]:
    """Run a command on a remote server via SSH.

    Returns (stdout, error). error is None on success.
    """
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="ignore")
        error = stderr.read().decode("utf-8", errors="ignore")
        if error:
            error = error.strip()
        return output, (error or None)
    except Exception as e:
        logging.error(f"Command failed: {command[:50]}... -> {e}")
        return "", str(e)


# --- Data Collection ---

def collect_server_info(server: Server, timeout: int = 15) -> ServerInfo:
    """Collect all information from a server."""
    info = ServerInfo(server=server, success=False, error=None, partial_errors=[])
    client = ssh_connect(server, timeout)
    if not client:
        info.error = f"SSH connection failed for {server.ip}:{server.port}"
        return info

    def probe(name: str, parser: Any) -> Any:
        """Run one probe; on failure record it and return None instead of raising."""
        raw, err = run_ssh_command(client, SSH_COMMANDS[name], timeout)
        if err:
            info.partial_errors.append(f"{name}: {err[:120]}")
        if not raw.strip():
            info.partial_errors.append(f"{name}: empty output")
            return None
        try:
            return parser(raw)
        except Exception as e:
            info.partial_errors.append(f"{name}: parse error {e}")
            return None

    try:
        info.os = probe("os", parse_os)
        info.cpu = probe("cpu_info", parse_cpu_info)

        usage_raw, usage_err = run_ssh_command(client, SSH_COMMANDS["cpu_usage"], timeout)
        if usage_err:
            info.partial_errors.append(f"cpu_usage: {usage_err[:120]}")
        if info.cpu is not None:
            info.cpu["usage"] = parse_cpu_usage(usage_raw) if usage_raw.strip() else None

        info.ram = probe("ram", parse_ram)
        info.disk = probe("disk", parse_disk)
        info.server_type = probe("server_type", parse_server_type)
        info.access_level = probe("access_level", parse_access_level)
        info.public_ip = probe("public_ip", parse_public_ip)
        info.bandwidth = probe("bandwidth", parse_bandwidth)
        info.ports = probe("ports", parse_ports)
        info.top_processes = probe("top_processes", parse_top_processes)

        info.success = True
    except Exception as e:
        info.error = str(e)
        logging.error(f"Error collecting info from {server.ip}: {e}")
    finally:
        client.close()

    return info


# --- Input Parsing ---

def parse_input_file(file_path: str) -> List[Server]:
    """Parse input file into Server objects."""
    servers = []
    with open(file_path, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Format: IP:PORT@USERNAME:PASS or IP@USERNAME:PASS
            if "@" not in line:
                logging.warning(f"Line {line_num}: Invalid format (missing @): {line}")
                continue
            try:
                # Split into IP:PORT and USERNAME:PASS
                creds_part, auth_part = line.split("@", 1)
                if ":" in creds_part:
                    ip, port = creds_part.rsplit(":", 1)
                    port = int(port)
                else:
                    ip = creds_part
                    port = 22
                if ":" in auth_part:
                    username, password = auth_part.split(":", 1)
                else:
                    username = auth_part
                    password = ""
                servers.append(Server(
                    ip=ip,
                    port=port,
                    username=username,
                    password=password,
                    raw_line=line,
                ))
            except Exception as e:
                logging.warning(f"Line {line_num}: Failed to parse: {line} -> {e}")
    return servers


# --- Output ---

def generate_table(report: Report) -> str:
    """Generate a markdown table from the report."""
    headers = [
        "Server", "OS", "CPU", "RAM (MB)", "Disk (GB)", "Type", 
        "Access", "Public IP", "Bandwidth", "Ports", "Top Processes", "Status"
    ]
    rows = []
    for info in report.servers:
        if not info.success:
            status = "❌"
        elif info.partial_errors:
            status = f"⚠️ {len(info.partial_errors)} partial"
        else:
            status = "✅"
        os_str = "?"
        if info.os:
            os_name = info.os.get('name', '?')
            os_version = info.os.get('version', '')
            os_str = f"{os_name} {os_version}".strip()
        cpu_str = "?"
        if info.cpu:
            model = info.cpu.get('model', '?')
            usage = info.cpu.get('usage')
            if usage is None:
                cpu_str = model
            else:
                cpu_str = f"{model} ({usage}%)"
        ram_str = "?"
        if info.ram:
            ram_str = str(info.ram.get('total', 0))
        disk_list = info.disk or []
        # Only real filesystems, not tmpfs/devtmpfs/efivarfs pseudo-mounts.
        disk_list = [d for d in disk_list if d["total"] > 0]
        root = next((d for d in disk_list if d["mount"] == "/"), None)
        if root:
            disk_str = f"/: {root['total']}G total, {root['free']}G free"
        elif disk_list:
            disk_str = ", ".join(f"{d['mount']}:{d['total']}G" for d in disk_list[:2])
        else:
            disk_str = "?"
        server_type = info.server_type or "?"
        access = info.access_level or "?"
        public_ip = info.public_ip or "?"
        ifaces = info.bandwidth.get("interfaces", []) if info.bandwidth else []
        ifaces = [i for i in ifaces if i["name"] != "lo"]
        known = [i for i in ifaces if i["speed"] != "NA"]
        if known:
            bandwidth = ", ".join(f"{i['name']}:{i['speed']}Mb" for i in known[:3])
        else:
            bandwidth = "?"
        ports = ", ".join(map(str, (info.ports or [])[:8])) or "?"
        top_procs = ", ".join([f"{p['name']}({p['cpu_percent']}%CPU)" for p in (info.top_processes or [])[:3]]) or "?"
        rows.append([
            f"{info.server.ip}:{info.server.port}",
            os_str,
            cpu_str,
            ram_str,
            disk_str,
            server_type,
            access,
            public_ip,
            bandwidth,
            ports,
            top_procs,
            status,
        ])
    
    # Calculate column widths
    col_widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    
    # Build table
    table = []
    table.append("|".join(f" {headers[i].ljust(col_widths[i])} " for i in range(len(headers))))
    table.append("|".join("-" * (col_widths[i] + 2) for i in range(len(headers))))
    for row in rows:
        table.append("|".join(f" {str(row[i]).ljust(col_widths[i])} " for i in range(len(headers))))
    
    return "\n".join(table)


def save_json(report: Report, output_path: Optional[str] = None) -> str:
    """Save report as JSON."""
    if output_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = f"report_{timestamp}.json"
    
    # asdict converts nested dataclasses automatically
    report_dict = asdict(report)
    
    with open(output_path, "w") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False, default=str)
    
    return output_path


# --- Parallel Execution ---

def run_parallel(servers: List[Server], max_workers: int = 10, timeout: int = 15) -> Report:
    """Run server monitoring in parallel batches."""
    report = Report(
        timestamp=datetime.now(timezone.utc).isoformat(),
        input_file="",
        servers=[],
        summary={"total": len(servers), "success": 0, "failed": 0, "partial": 0},
    )
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_server_info, server, timeout): server 
            for server in servers
        }
        for future in as_completed(futures):
            server = futures[future]
            try:
                info = future.result()
                report.servers.append(info)
                if info.success:
                    if info.partial_errors:
                        report.summary["partial"] += 1
                    else:
                        report.summary["success"] += 1
                else:
                    report.summary["failed"] += 1
            except Exception as e:
                logging.error(f"Error processing {server.ip}: {e}")
                report.servers.append(ServerInfo(
                    server=server,
                    success=False,
                    error=str(e),
                ))
                report.summary["failed"] += 1
    
    return report


# --- Main ---

def main():
    _bootstrap()
    parser = argparse.ArgumentParser(description="Server Monitor - Parallel SSH")
    parser.add_argument("input_file", help="Path to input file (IP:PORT@USERNAME:PASS per line)")
    parser.add_argument("--output", help="Custom JSON output path")
    parser.add_argument("--quiet", action="store_true", help="Suppress table output")
    parser.add_argument("--max-workers", type=int, default=10, help="Max concurrent SSH sessions (default: 10)")
    parser.add_argument("--timeout", type=int, default=15, help="SSH timeout in seconds (default: 15)")
    parser.add_argument("--log", help="Log file path (default: server_monitor.log)")
    args = parser.parse_args()

    # Setup logging
    log_path = args.log or "server_monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Parse input
    servers = parse_input_file(args.input_file)
    if not servers:
        logging.warning("No valid servers found in input file.")
        return

    logging.info(f"Processing {len(servers)} servers with {args.max_workers} parallel workers...")

    # Run parallel monitoring
    report = run_parallel(servers, args.max_workers, args.timeout)
    report.input_file = args.input_file

    # Output
    if not args.quiet:
        print("\n" + generate_table(report))

    json_path = save_json(report, args.output)
    logging.info(f"JSON report saved to: {json_path}")
    logging.info(f"Summary: {report.summary}")


if __name__ == "__main__":
    main()
