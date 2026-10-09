"""Run on the NAS before starting CamMon; Python standard library only."""
import argparse
import os
import shutil
import socket
from pathlib import Path

parser = argparse.ArgumentParser(description="CamMon NAS 部署检查")
parser.add_argument("--port", type=int, default=18080)
parser.add_argument("--data", type=Path, help="兼容旧版参数；元数据现为内存模式，不检查持久目录")
parser.add_argument("--cache", type=Path, default=Path("cache"))
args = parser.parse_args()
errors = []
for transport, ports, label in ((socket.SOCK_STREAM, [139, 445, args.port], "TCP"),
                                (socket.SOCK_DGRAM, [137, 138], "UDP")):
    for port in ports:
        with socket.socket(socket.AF_INET, transport) as probe:
            try:
                probe.bind(("0.0.0.0", port))
                print(f"可用：{label} {port}")
            except OSError as exc:
                errors.append(f"{label} {port}: {exc}")
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
