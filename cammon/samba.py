import grp
import logging
import os
import pwd
import signal
import socket
import subprocess

log = logging.getLogger(__name__)


def check_ports(*, include_netbios: bool = True) -> list[str]:
    conflicts = []
    transports = [(socket.SOCK_STREAM, (139, 445))]
    if include_netbios:
        transports.append((socket.SOCK_DGRAM, (137, 138)))
    for transport, ports in transports:
        for port in ports:
            with socket.socket(socket.AF_INET, transport) as probe:
                try:
                    probe.bind(("0.0.0.0", port))
                except OSError as exc:
                    conflicts.append(f"{'UDP' if transport == socket.SOCK_DGRAM else 'TCP'} {port}: {exc}")
    return conflicts


class SambaManager:
    def __init__(self, gateway):
        self.gateway = gateway
        self.settings = gateway.settings
        self.processes: list[subprocess.Popen] = []
        self.group_id = 28000
        self.discovery_mode = "pending"

    def prepare(self):
        if os.geteuid() != 0:
            raise RuntimeError("Samba requires root inside the ordinary container to manage isolated accounts")
        try:
            self.group_id = grp.getgrnam("cammon").gr_gid
        except KeyError:
            subprocess.run(["groupadd", "--gid", str(self.group_id), "cammon"], check=True)
        directory = self.settings.data_dir / "samba"
        for name in ("", "private", "state", "cache", "lock"):
            path = directory / name
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700 if name else 0o755)
        self.render()
        for device in self.gateway.devices():
            self._unix_user(device["username"], device["uid"])
            password = self.gateway.device_password(device["id"])
            if password:
                self.set_password(device["username"], password)
            else:
                self.gateway.worker_error = f"设备 {device['name']} 缺少接入凭据，请在管理页重置密码"

    def _unix_user(self, username: str, uid: int | None = None):
        try:
            account = pwd.getpwnam(username)
            if uid is not None and account.pw_uid != uid:
                raise RuntimeError(f"Persistent camera UID conflict: {username}")
            return account
        except KeyError:
            command = ["useradd", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
                       "--gid", "cammon"]
            if uid is not None:
                command += ["--uid", str(uid)]
            subprocess.run(command + [username], check=True, capture_output=True)
            return pwd.getpwnam(username)

    def create_user(self, username: str, password: str) -> tuple[int, int]:
        account = self._unix_user(username)
        self.set_password(username, password)
        return account.pw_uid, account.pw_gid

    def set_password(self, username: str, password: str):
        subprocess.run([str(self.settings.samba_prefix / "bin/smbpasswd"), "-c", str(self.settings.samba_config),
                        "-a", "-s", username], input=password + "\n" + password + "\n", text=True,
                       check=True, capture_output=True)

    def render(self):
        directory = self.settings.data_dir / "samba"
        directory.mkdir(parents=True, exist_ok=True)
        config = f"""[global]
    workgroup = WORKGROUP
    netbios name = CAMMON
    server string = CamMon Camera Storage
    server role = standalone server
    security = user
    map to guest = Never
    server min protocol = NT1
    server max protocol = SMB3
    ntlm auth = {'ntlmv1-permitted' if self.settings.allow_ntlmv1 else 'ntlmv2-only'}
    smb1 unix extensions = no
    load printers = no
    disable spoolss = yes
    printing = bsd
    printcap name = /dev/null
    private dir = {directory / 'private'}
    state directory = {directory / 'state'}
    cache directory = {directory / 'cache'}
    lock directory = {directory / 'lock'}
    pid directory = {self.settings.socket_path.parent}
    log file = /dev/stdout
    logging = file
    max log size = 0
    smb2 leases = no
    min receivefile size = 0
    server multi channel support = no
    ea support = no
    local master = yes
    preferred master = no
"""
        for device in self.gateway.devices():
            if not device["enabled"]:
                continue
            config += f"""
[{device['share']}]
    path = {self.settings.shares / device['id']}
    valid users = {device['username']}
    read only = no
    guest ok = no
    browseable = yes
    access based share enum = yes
    vfs objects = cammon
    cammon:device = {device['id']}
    cammon:socket = {self.settings.socket_path}
    use sendfile = no
    aio read size = 0
    aio write size = 0
    strict allocate = no
    oplocks = no
    kernel oplocks = no
    store dos attributes = no
    follow symlinks = no
    wide links = no
    create mask = 0600
    directory mask = 0700
"""
        temporary = self.settings.samba_config.with_suffix(".new")
        temporary.write_text(config)
        temporary.replace(self.settings.samba_config)
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGHUP)

    def start(self):
        conflicts = check_ports(include_netbios=self.settings.netbios_mode != "external")
        udp_conflicts = [conflict for conflict in conflicts if conflict.startswith("UDP ")]
        required_conflicts = [conflict for conflict in conflicts if conflict.startswith("TCP ")]
        if self.settings.netbios_mode == "internal":
            required_conflicts += udp_conflicts
        if required_conflicts:
            raise RuntimeError("NAS 上的 SMB/发现端口被占用，请先释放这些端口: " + "; ".join(required_conflicts))
        self.discovery_mode = (
            "external" if self.settings.netbios_mode == "external" or udp_conflicts else "internal"
        )
        if self.discovery_mode == "external":
            log.info("NetBIOS discovery uses the host service or direct IP access; CamMon nmbd is disabled%s",
                     ": " + "; ".join(udp_conflicts) if udp_conflicts else "")
        names = ("smbd", "nmbd") if self.discovery_mode == "internal" else ("smbd",)
        for name in names:
            process = subprocess.Popen([
                str(self.settings.samba_prefix / "sbin" / name), "-F", "--no-process-group",
                "-s", str(self.settings.samba_config),
            ], start_new_session=True)
            self.processes.append(process)

    def discovery_status(self) -> dict:
        messages = {
            "pending": "摄像机接入服务尚未启动",
            "internal": "CamMon 提供局域网发现，名称为 CAMMON",
            "external": "使用 NAS 现有发现服务，请通过 NAS 名称或 IP 接入",
        }
        return {"mode": self.discovery_mode, "message": messages[self.discovery_mode]}

    def health(self) -> str | None:
        for process in self.processes:
            if process.poll() is not None:
                return f"{process.args[0]} exited with code {process.returncode}"
        return None

    def stop(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
