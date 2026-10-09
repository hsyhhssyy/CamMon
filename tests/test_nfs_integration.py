"""NFSv3/v4.0 wire tests against a user-space Ganesha MEM export, without mounts."""
import os
import re
import shutil
import socket
import subprocess
import time

import pytest

from cammon.storage import NFSBackend, file_hash
from tests.backend_faults import verify_interrupted_upload_and_retry

pytestmark = [pytest.mark.integration, pytest.mark.nfs]


@pytest.fixture(scope="module")
def nfs_server(tmp_path_factory):
    if not os.environ.get("CAMMON_TEST_NFS") or os.geteuid() != 0 or not shutil.which("ganesha.nfsd"):
        pytest.skip("Set CAMMON_TEST_NFS=1 in the native test environment with Ganesha MEM installed")
    pytest.importorskip("cammon._nfs")
    directory = tmp_path_factory.mktemp("ganesha")
    rpc = None
    for port in (111, 2049):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                pytest.fail(f"Native NFS tests need exclusive loopback port {port}")
    config = directory / "ganesha.conf"
    version = subprocess.run(["ganesha.nfsd", "-v"], capture_output=True, text=True, check=True)
    major = re.search(r"V(\d+)\.", version.stdout + version.stderr)
    extra = "allow_set_io_flusher_fail = true;" if major and int(major[1]) >= 6 else ""
    config.write_text("""NFS_CORE_PARAM { Protocols = 3,4; Bind_addr = 127.0.0.1; mount_path_pseudo = true; Enable_NLM = false; Enable_RQUOTA = false; EXTRA }
NFSv4 { Graceless = true; }
EXPORT { Export_Id = 77; Path = /cammon; Pseudo = /cammon; Protocols = 3,4; Access_Type = RW;
         Squash = No_Root_Squash; SecType = sys; FSAL { Name = MEM; } }
MEM { Inode_Size = 2097152; }
""".replace("EXTRA", extra))
    rpc = subprocess.Popen(["rpcbind", "-f", "-w", "-h", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process = None
    try:
        time.sleep(0.3)
        process = subprocess.Popen(["ganesha.nfsd", "-F", "-f", str(config), "-L", str(directory / "server.log"),
                                    "-p", str(directory / "server.pid")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail((directory / "server.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", 2049), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Ganesha did not open NFS port")
        yield "nfs://127.0.0.1/cammon"
    finally:
        for child in (process, rpc):
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


@pytest.mark.parametrize("version", [3, 4])
def test_nfs_real_upload_verify_rename_read_and_delete(nfs_server, tmp_path, version):
    backend = NFSBackend(dict(address=nfs_server, nfs_version=version, uid=0, gid=0), timeout=10)
    source = tmp_path / "input.mp4"
    source.write_bytes(bytes(range(256)) * 1024)
    temporary = f"version{version}/nested/input.cammon-part"
    destination = f"version{version}/nested/input.mp4"
    backend.upload(temporary, source)
    assert backend.stat(temporary) == source.stat().st_size
    assert backend.sha256(temporary) == file_hash(source)
    backend.rename(temporary, destination)
    assert not backend.exists(temporary)
    with backend.reader(destination) as stream:
        stream.seek(100)
        assert stream.read(8192) == source.read_bytes()[100:8292]
        # Other contexts must not invalidate a live NFSv4 reader's open state.
        assert backend.stat(destination) == source.stat().st_size
        assert backend.sha256(destination) == file_hash(source)
        stream.seek(1000)
        assert stream.read(4096) == source.read_bytes()[1000:5096]
    assert backend.probe(tmp_path)["ok"]
    backend.remove(destination)
    assert not backend.exists(destination)


@pytest.mark.parametrize("version", [3, 4])
def test_nfs_backend_interruption_space_and_restart(nfs_server, gateway, device, monkeypatch, version):
    gateway.backend = NFSBackend(dict(address=nfs_server, nfs_version=version, uid=0, gid=0), timeout=10)
    verify_interrupted_upload_and_retry(gateway, device, monkeypatch)
