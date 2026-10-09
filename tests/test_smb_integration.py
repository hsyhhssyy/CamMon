"""Real SMB1 packets against the compiled module and real SMB2/3 backend traffic.

Run as root in the build/test environment, not with a privileged container:
  CAMMON_TEST_SAMBA_PREFIX=/opt/samba python -m pytest -m smb
"""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from impacket.nmb import NetBIOS
from impacket.smb import SMB_DIALECT
from impacket.smb3structs import FILE_CREATE, FILE_OPEN, GENERIC_READ, GENERIC_WRITE
from impacket.smbconnection import SessionError, SMBConnection

from cammon.config import Settings
from cammon.core import Gateway
from cammon.rpc import Server
from cammon.samba import SambaManager
from cammon.storage import SMBBackend
from tests.backend_faults import verify_interrupted_upload_and_retry

pytestmark = [pytest.mark.integration, pytest.mark.smb]


@pytest.fixture
def smb_gateway(tmp_path):
    prefix = Path(os.environ.get("CAMMON_TEST_SAMBA_PREFIX", "/opt/samba"))
    if os.geteuid() != 0 or not (prefix / "sbin/smbd").exists():
        pytest.skip("Needs root and a matching Samba/VFS build; see the native integration instructions")
    for path in (tmp_path, *tmp_path.parents):
        if path == Path("/tmp"):
            break
        path.chmod(0o755)
    settings = Settings(_env_file=None, postgres_dsn=None, secret_key=None,
                        data_dir=tmp_path / "data", socket_path=tmp_path / "vfs.sock",
                        admin_password="native-test-admin-password", samba_prefix=prefix, worker_enabled=False,
                        cache_reserve_bytes=0)
    gateway = Gateway(settings)
    manager = SambaManager(gateway)
    gateway.samba = manager
    manager.prepare()
    device = gateway.create_device("SMB1 camera")
    other = gateway.create_device("Isolated camera")
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    remote = tmp_path / "real-smb-backend"
    remote.mkdir(mode=0o700)
    os.chown(remote, device["uid"], device["gid"])
    config = settings.samba_config.read_text().replace("[global]", f"[global]\n    smb ports = {port}\n"
                                                     "    interfaces = 127.0.0.1\n    bind interfaces only = yes")
    config = config.replace("log file = /dev/stdout", f"log file = {tmp_path / 'samba.log'}")
    config = config.replace("[global]", "[global]\n    log level = 3")
    config += f"\n[backend]\npath = {remote}\nvalid users = {device['username']}\nread only = no\n"
    settings.samba_config.write_text(config)
    rpc = Server(gateway)
    rpc.start()
    process = subprocess.Popen([str(prefix / "sbin/smbd"), "-F", "--no-process-group", "-s", str(settings.samba_config)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail((tmp_path / "samba.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Samba did not open its test port")
        gateway.backend = SMBBackend(dict(protocol="smb", address=f"smb://127.0.0.1:{port}/backend",
                                          username=device["username"], password=device["password"]))
        yield gateway, device, other, port, remote
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        rpc.stop()
        gateway.shutdown()
        for created in (device, other):
            subprocess.run(["userdel", created["username"]], capture_output=True)


def connect(device, port):
    client = SMBConnection("127.0.0.1", "127.0.0.1", sess_port=port, preferredDialect=SMB_DIALECT)
    client.login(device["username"], device["password"])
    assert client.getDialect() == SMB_DIALECT
    return client


def test_real_smb1_write_header_rename_flush_and_remote_read(smb_gateway):
    gateway, device, other, port, remote = smb_gateway
    client = connect(device, port)
    try:
        share = device["share"]
        client.createDirectory(share, "day")
        client.createDirectory(share, "day\\hour")
        with pytest.raises(SessionError):
            client.createDirectory(share, "day\\hour\\minute")
        tree = client.connectTree(share)
        first = "day\\hour\\00_20251008080000_20251008080100.mp4"
        renamed = "day\\hour\\00_20251008080000_20251008080200.mp4"
        with pytest.raises(SessionError):
            client.openFile(tree, "bad.txt", desiredAccess=GENERIC_WRITE, creationDisposition=FILE_CREATE)
        file_id = client.openFile(tree, first, desiredAccess=GENERIC_READ | GENERIC_WRITE, creationDisposition=FILE_CREATE)
        assert client.writeFile(tree, file_id, b"0000FRAME", 0) == 9
        assert client.writeFile(tree, file_id, b"MOOV", 0) == 4
        assert client.readFile(tree, file_id, 0, 9) == b"MOOVFRAME"
        entries = client.listPath(share, "day\\hour\\*")
        assert next(e for e in entries if e.get_longname().endswith(".mp4")).get_filesize() == 9
        client.closeFile(tree, file_id)
        client.rename(share, first, renamed)
        assert gateway.recording(1)["end"] - gateway.recording(1)["start"] == 120
        assert gateway.seal_candidates(time.time() + 1900) == [1]
        assert gateway.flush_one(1), gateway.recording(1)["error"]
        assert (remote / gateway.recording(1)["remote_path"]).read_bytes() == b"MOOVFRAME"
        assert not gateway.blob(1).exists()
        reader = client.openFile(tree, renamed, desiredAccess=GENERIC_READ, creationDisposition=FILE_OPEN)
        assert client.readFile(tree, reader, 0, 9) == b"MOOVFRAME"
        client.closeFile(tree, reader)
        assert next(e for e in client.listPath(share, "day\\hour\\*") if e.get_longname().endswith(".mp4")).get_filesize() == 9
        with pytest.raises(SessionError):
            client.openFile(tree, renamed, desiredAccess=GENERIC_WRITE, creationDisposition=FILE_OPEN)
        with pytest.raises(SessionError):
            client.deleteFile(share, renamed)
        with pytest.raises(SessionError):
            client.connectTree(other["share"])
    finally:
        client.close()


def test_real_smb1_disconnect_releases_writer_and_keeps_channel_independence(smb_gateway):
    gateway, device, _, port, _ = smb_gateway
    client = connect(device, port)
    share = device["share"]
    client.createDirectory(share, "day")
    tree = client.connectTree(share)
    first = client.openFile(tree, "day\\00_20251008080000_20251008080100.mp4",
                           desiredAccess=GENERIC_READ | GENERIC_WRITE, creationDisposition=FILE_CREATE)
    client.writeFile(tree, first, b"payload", 0)
    other = client.openFile(tree, "day\\01_20251008080000_20251008080100.mp4",
                           desiredAccess=GENERIC_READ | GENERIC_WRITE, creationDisposition=FILE_CREATE)
    client.closeFile(tree, other)
    assert gateway.seal_candidates(time.time() + 20) == []
    client.close()
    deadline = time.time() + 5
    while gateway.handles and time.time() < deadline:
        time.sleep(0.1)
    assert not gateway.handles
    assert gateway.seal_candidates(time.time() + 1900) == [1, 2]


def test_smb_backend_interruption_space_and_restart(smb_gateway, monkeypatch):
    gateway, device, _, _, _ = smb_gateway
    gateway.mkdir(device["id"], "day")
    verify_interrupted_upload_and_retry(gateway, device, monkeypatch)


def test_index_directory_pages_exclude_unindexed_shadows_and_support_directory_rename(smb_gateway):
    gateway, device, _, port, _ = smb_gateway
    gateway.mkdir(device["id"], "day")
    names = set()
    for index in range(140):
        name = f"{index:03d}_20251008080000_20251008080100.mp4"
        token = gateway.open(device["id"], "day/" + name, os.O_CREAT | os.O_RDWR)
        gateway.write(token, b"data", 0)
        gateway.close(token)
        names.add(name)
    gateway._projection(device, "day/unindexed.txt").touch()
    client = connect(device, port)
    try:
        entries = client.listPath(device["share"], "day\\*")
        assert {entry.get_longname() for entry in entries if entry.get_longname() not in (".", "..")} == names
        client.rename(device["share"], "day", "renamed")
        entries = client.listPath(device["share"], "renamed\\*")
        assert {entry.get_longname() for entry in entries if entry.get_longname() not in (".", "..")} == names
    finally:
        client.close()


def test_netbios_discovery_and_port_conflict_report(smb_gateway):
    from cammon.samba import check_ports

    gateway, _, _, _, _ = smb_gateway
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 445))
        assert any("TCP 445" in conflict for conflict in check_ports())
    config = gateway.settings.samba_config.read_text().replace("    interfaces = 127.0.0.1\n", "").replace(
        "    bind interfaces only = yes\n", "")
    path = gateway.settings.data_dir / "samba/nmb-test.conf"
    path.write_text(config)
    process = subprocess.Popen([str(gateway.settings.samba_prefix / "sbin/nmbd"), "-F", "--no-process-group", "-s", str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    client = NetBIOS()
    try:
        address = socket.gethostbyname(socket.gethostname())
        deadline = time.time() + 10
        names = None
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail("nmbd exited before discovery")
            try:
                names = client.getnodestatus("*", address, timeout=0.5)
                break
            except Exception:
                time.sleep(0.2)
        assert names, "No NetBIOS discovery reply"
        assert any(entry["NAME"].strip() == b"CAMMON" for entry in names)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
