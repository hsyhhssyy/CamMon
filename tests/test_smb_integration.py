"""Real SMB1 packets against the compiled module and real SMB2/3 backend traffic.

Run as root in the build/test environment, not with a privileged container:
  CAMMON_TEST_SAMBA_PREFIX=/opt/samba python -m pytest -m smb
"""

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from impacket.nmb import NetBIOS
from impacket.smb import SMB_DIALECT
from impacket.smb3structs import FILE_CREATE, FILE_OPEN, GENERIC_READ, GENERIC_WRITE, SMB2_DIALECT_311
from impacket.smbconnection import SessionError, SMBConnection

from cammon.api import create_app
from cammon.archive import Archiver
from cammon.config import Settings
from cammon.core import Gateway
from cammon.rpc import Server
from cammon.samba import SambaManager
from cammon.storage import SMBBackend
from tests.backend_faults import verify_interrupted_upload_and_retry
from tests.test_archive import instant

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
                                                     f"    ncalrpc dir = {tmp_path / 'ncalrpc'}\n"
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


def connect(device, port, dialect=SMB_DIALECT):
    client = SMBConnection("127.0.0.1", "127.0.0.1", sess_port=port, preferredDialect=dialect)
    client.login(device["username"], device["password"])
    assert client.getDialect() == dialect
    return client


@pytest.mark.parametrize("dialect", [SMB_DIALECT, SMB2_DIALECT_311], ids=["smb1", "smb3"])
def test_share_enumeration_only_lists_the_logged_in_camera(smb_gateway, dialect):
    _, device, other, port, _ = smb_gateway
    camera_shares = {device["share"], other["share"]}
    for owner, forbidden in ((device, other), (other, device)):
        client = connect(owner, port, dialect)
        try:
            visible = {entry["shi1_netname"].rstrip("\x00") for entry in client.listShares()}
            assert visible & camera_shares == {owner["share"]}, visible
            tree = client.connectTree(owner["share"])
            client.disconnectTree(tree)
            with pytest.raises(SessionError):
                client.connectTree(forbidden["share"])
        finally:
            client.close()


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


def test_real_smb1_archived_original_keeps_its_name_and_size(smb_gateway, tmp_path):
    gateway, device, _, port, remote = smb_gateway
    sample = tmp_path / "video.mp4"
    subprocess.run([gateway.settings.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x48:r=1", "-t", "60", "-an", "-c:v", "libx264",
                    "-threads", "1", str(sample)], check=True)
    data = sample.read_bytes()
    client = connect(device, port)
    try:
        client.createDirectory(device["share"], "day")
        tree = client.connectTree(device["share"])
        name = "day\\00_20251008080000_20251008080100.mp4"
        writer = client.openFile(tree, name, desiredAccess=GENERIC_READ | GENERIC_WRITE,
                                 creationDisposition=FILE_CREATE)
        assert client.writeFile(tree, writer, data, 0) == len(data)
        client.closeFile(tree, writer)
        assert gateway.seal_candidates(time.time() + 1900) == [1]
        assert gateway.flush_one(1)
        original = remote / gateway.recording(1)["remote_path"]
        archiver = Archiver(gateway)
        assert archiver.enqueue(instant("2025-12-01T02:00:00")) == 1
        job = gateway.db.one("SELECT id FROM archive_jobs")
        assert archiver.process(job["id"]), gateway.db.one("SELECT error FROM archive_jobs")["error"]
        assert gateway.recording(1)["state"] == "virtual"
        assert not original.exists()
        assert not gateway.blob(1).exists()
        entry = next(e for e in client.listPath(device["share"], "day\\*") if e.get_longname().endswith(".mp4"))
        assert entry.get_filesize() == len(data)
        reader = client.openFile(tree, name, desiredAccess=GENERIC_READ, creationDisposition=FILE_OPEN)
        assert client.queryInfo(tree, reader)["EndOfFile"] == len(data)
        with pytest.raises(SessionError):
            client.readFile(tree, reader, 0, 16)
        assert client.queryInfo(tree, reader)["EndOfFile"] == len(data)
        client.closeFile(tree, reader)
        for access, disposition in ((GENERIC_WRITE, FILE_OPEN), (GENERIC_WRITE, FILE_CREATE)):
            with pytest.raises(SessionError):
                client.openFile(tree, name, desiredAccess=access, creationDisposition=disposition)
        with pytest.raises(SessionError):
            client.deleteFile(device["share"], name)
        assert gateway.query_files({"kind": "original"})["count"] == 0
    finally:
        client.close()


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


@pytest.fixture
def managed_smb_gateway(tmp_path):
    prefix = Path(os.environ.get("CAMMON_TEST_SAMBA_PREFIX", "/opt/samba"))
    if os.geteuid() != 0 or not (prefix / "sbin/smbd").exists():
        pytest.skip("Needs root and a matching Samba/VFS build")
    for path in (tmp_path, *tmp_path.parents):
        if path == Path("/tmp"):
            break
        path.chmod(0o755)
    settings = Settings(_env_file=None, postgres_dsn=None, secret_key=None,
                        data_dir=tmp_path / "data", socket_path=tmp_path / "vfs.sock",
                        admin_password="native-test-admin-password", samba_prefix=prefix,
                        worker_enabled=False, cache_reserve_bytes=0)
    gateway = Gateway(settings)
    gateway.samba = SambaManager(gateway)
    gateway.samba.prepare()
    device = gateway.create_device("Camera using NAS discovery")
    try:
        yield gateway, device
    finally:
        gateway.shutdown()
        subprocess.run(["userdel", device["username"]], capture_output=True)


def wait_managed_smb(gateway):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert not gateway.protocol_error, gateway.protocol_error
        assert not gateway.samba.health(), gateway.samba.health()
        try:
            for port in (139, 445):
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            return
        except OSError:
            time.sleep(0.1)
    pytest.fail("Managed smbd did not open TCP 139 and 445")


def verify_managed_smb1(device, port):
    # A camera may use the NAS's existing name, rather than CAMMON, on TCP 139.
    camera = SMBConnection("NAS-EXISTING", "127.0.0.1", sess_port=port, preferredDialect=SMB_DIALECT)
    try:
        camera.login(device["username"], device["password"])
        assert camera.getDialect() == SMB_DIALECT
        camera.createDirectory(device["share"], f"day{port}")
        tree = camera.connectTree(device["share"])
        handle = camera.openFile(tree, f"day{port}\\00_20251008080000_20251008080100.mp4",
                                 desiredAccess=GENERIC_READ | GENERIC_WRITE, creationDisposition=FILE_CREATE)
        assert camera.writeFile(tree, handle, b"0000FRAME", 0) == 9
        assert camera.writeFile(tree, handle, b"MOOV", 0) == 4
        assert camera.readFile(tree, handle, 0, 9) == b"MOOVFRAME"
        camera.closeFile(tree, handle)
    finally:
        camera.close()


@pytest.mark.parametrize("mode,ports", [
    ("auto", (137,)), ("auto", (138,)), ("auto", (137, 138)), ("external", ()),
    ("auto", ()), ("internal", ()),
])
def test_smb1_starts_with_host_udp_ports_and_reports_healthy(managed_smb_gateway, mode, ports):
    gateway, device = managed_smb_gateway
    gateway.settings.netbios_mode = mode
    with ExitStack() as stack:
        owners = []
        for port in ports:
            owner = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            owner.bind(("0.0.0.0", port))
            owner.settimeout(2)
            owners.append(owner)
        preflight = subprocess.run([sys.executable, "scripts/check_deployment.py", "--port", "0",
                                    "--netbios-mode", mode, "--cache", str(gateway.settings.cache)],
                                   capture_output=True, text=True)
        assert preflight.returncode == 0, preflight.stdout + preflight.stderr
        api = stack.enter_context(TestClient(create_app(gateway.settings, gateway)))
        wait_managed_smb(gateway)
        expected = "external" if ports or mode == "external" else "internal"
        assert api.get("/healthz").status_code == 200
        assert api.get("/healthz").json()["discovery"]["mode"] == expected
        assert len(gateway.samba.processes) == (1 if expected == "external" else 2)
        for port in (139, 445):
            verify_managed_smb1(device, port)
        # The original UDP listeners continue to receive their own traffic.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for owner in owners:
                sender.sendto(b"host-service", ("127.0.0.1", owner.getsockname()[1]))
                assert owner.recvfrom(64)[0] == b"host-service"
        if expected == "external" and not ports:
            for port in (137, 138):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind(("0.0.0.0", port))


@pytest.mark.parametrize("mode,transport,port", [
    ("internal", socket.SOCK_DGRAM, 137),
    ("auto", socket.SOCK_STREAM, 445),
    ("external", socket.SOCK_STREAM, 139),
])
def test_required_ports_still_block_startup(managed_smb_gateway, mode, transport, port):
    gateway, _ = managed_smb_gateway
    gateway.settings.netbios_mode = mode
    with socket.socket(socket.AF_INET, transport) as owner:
        owner.bind(("0.0.0.0", port))
        preflight = subprocess.run([sys.executable, "scripts/check_deployment.py", "--port", "0",
                                    "--netbios-mode", mode, "--cache", str(gateway.settings.cache)],
                                   capture_output=True, text=True)
        assert preflight.returncode == 1, preflight.stdout + preflight.stderr
        with TestClient(create_app(gateway.settings, gateway)) as api:
            response = api.get("/healthz")
            assert response.status_code == 503
            label = "UDP" if transport == socket.SOCK_DGRAM else "TCP"
            assert f"{label} {port}" in response.json()["protocol_error"]
            assert not gateway.samba.processes


def test_existing_nas_netbios_discovery_survives_smb1_startup(managed_smb_gateway):
    gateway, device = managed_smb_gateway
    host_config = gateway.settings.samba_config.with_name("host-netbios.conf")
    host_config.write_text(gateway.settings.samba_config.read_text().replace(
        "netbios name = CAMMON", "netbios name = NAS-EXISTING"
    ))
    host = subprocess.Popen([str(gateway.settings.samba_prefix / "sbin/nmbd"), "-F", "--no-process-group",
                             "-s", str(host_config)], start_new_session=True)
    discovery = NetBIOS()
    address = socket.gethostbyname(socket.gethostname())

    def host_names():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert host.poll() is None, "Host nmbd exited"
            try:
                names = discovery.getnodestatus("*", address, timeout=0.5)
                assert any(entry["NAME"].strip() == b"NAS-EXISTING" for entry in names)
                return
            except Exception:
                time.sleep(0.2)
        pytest.fail("Existing NAS NetBIOS discovery did not reply")

    try:
        host_names()
        with TestClient(create_app(gateway.settings, gateway)) as api:
            wait_managed_smb(gateway)
            assert api.get("/healthz").json()["discovery"]["mode"] == "external"
            assert api.get("/healthz").status_code == 200
            for port in (139, 445):
                verify_managed_smb1(device, port)
            host_names()
            assert host.poll() is None
    finally:
        if host.poll() is None:
            os.killpg(host.pid, signal.SIGTERM)
            host.wait(timeout=10)
