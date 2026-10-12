"""Run on the NAS before starting CamMon; Python standard library only."""
import argparse
import os
import shutil
import socket
from pathlib import Path

parser = argparse.ArgumentParser(description="CamMon NAS 部署检查")
parser.add_argument("--port", type=int, default=18080)
parser.add_argument("--netbios-mode", choices=("auto", "internal", "external"),
                    default=os.environ.get("CAMMON_NETBIOS_MODE", "auto"))
parser.add_argument("--data", type=Path, help="兼容旧版参数；元数据现为内存模式，不检查持久目录")
parser.add_argument("--cache", type=Path, default=Path("cache"))
args = parser.parse_args()
errors = []
udp_conflicts = []
for transport, ports, label in ((socket.SOCK_STREAM, [139, 445, args.port], "TCP"),
                                (socket.SOCK_DGRAM, [137, 138], "UDP")):
    if transport == socket.SOCK_DGRAM and args.netbios_mode == "external":
        continue
    for port in ports:
        with socket.socket(socket.AF_INET, transport) as probe:
            try:
                probe.bind(("0.0.0.0", port))
                print(f"可用：{label} {port}")
            except OSError as exc:
                if transport == socket.SOCK_DGRAM and args.netbios_mode == "auto":
                    udp_conflicts.append(f"{label} {port}: {exc}")
                else:
                    errors.append(f"{label} {port}: {exc}")
if args.netbios_mode == "external" or udp_conflicts:
    print("发现服务：使用 NAS 现有 NetBIOS 服务或直接通过 IP 接入；CamMon 不占用 UDP 137/138。")
else:
    print("发现服务：使用 CamMon 自带 nmbd。")
for path in (args.cache,):
    existing = path.resolve()
    while not existing.exists():
        existing = existing.parent
    if not os.access(existing, os.W_OK):
        errors.append(f"目录不可写：{path}")
    print(f"目录：{path.resolve()}，所在磁盘剩余 {shutil.disk_usage(existing).free:,} 字节")
if errors:
    print("检查失败：\n" + "\n".join(errors))
    raise SystemExit(1)
print("检查通过。元数据仅在内存，Samba 运行文件使用容器 tmpfs；启动时再次检查端口。")
