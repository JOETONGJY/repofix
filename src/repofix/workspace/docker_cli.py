"""Docker CLI subprocess 封装（plan.md：subprocess + 参数数组，不用 docker-py）。

所有调用均为 argv 数组、不经 Shell 字符串拼接；超时由本层控制。
"""
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repofix.workspace.errors import WorkspaceError

# Docker Desktop 安装器更新的系统 PATH 对已启动进程不生效：提供候选位置
_DOCKER_FALLBACKS = [
    r"E:\Docker\resources\bin\docker.exe",
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
]


def resolve_docker_executable() -> str:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _DOCKER_FALLBACKS:
        if Path(candidate).exists():
            return candidate
    return "docker"


class DockerCliError(WorkspaceError):
    """docker CLI 调用失败（基础设施层）。"""


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class DockerCli:
    def __init__(self, executable: str | None = None):
        self._docker = executable or os.environ.get("REPOFIX_DOCKER_CLI") or resolve_docker_executable()

    def run(
        self,
        args: list[str],
        timeout: int = 120,
        check: bool = False,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> CliResult:
        try:
            proc = subprocess.run(
                [self._docker, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                input=input_text,
            )
        except FileNotFoundError as exc:
            raise DockerCliError(f"docker executable not found: {self._docker}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerCliError(f"docker command timed out after {timeout}s: {' '.join(args[:3])}") from exc
        result = CliResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and proc.returncode != 0:
            raise DockerCliError(
                f"docker {' '.join(args[:3])} failed rc={proc.returncode}: {result.stderr.strip()[:300]}"
            )
        return result

    # ---- 高层便捷方法 ----

    def version(self) -> str:
        return self.run(["version", "--format", "{{.Server.Version}}"], timeout=30, check=True).stdout.strip()

    def image_exists(self, image: str) -> bool:
        return self.run(["image", "inspect", image], timeout=30).returncode == 0

    def container_exists(self, name: str) -> bool:
        return self.run(["container", "inspect", name], timeout=30).returncode == 0

    def start_container(
        self,
        name: str,
        image: str,
        task_dir: Path,
        *,
        memory: str = "1g",
        cpus: str = "1.0",
        pids_limit: int = 100,
        network: str = "none",
        user: str = "1000:1000",
    ) -> None:
        """启动受限容器（design/05 安全边界：非 root / 无网 / 资源限制 / 仅挂载任务副本）。

        同名容器残留（崩溃恢复场景）先移除——初始化幂等。
        """
        self.remove_container(name)
        self.run(
            [
                "run",
                "-d",
                "--name", name,
                "--user", user,
                "--network", network,
                "--memory", memory,
                "--cpus", cpus,
                "--pids-limit", str(pids_limit),
                "--security-opt", "no-new-privileges",
                "--volume", f"{task_dir}:/workspace",
                "--workdir", "/workspace",
                image,
                "sleep", "infinity",
            ],
            timeout=120,
            check=True,
        )

    def exec(self, name: str, argv: list[str], timeout: int = 120, cwd_in_container: str = "/workspace") -> CliResult:
        """容器内执行命令（argv 数组；超时抛 DockerCliError——调用方负责终止进程树）。"""
        return self.run(
            ["exec", "--workdir", cwd_in_container, name, *argv],
            timeout=timeout,
        )

    def stop_container(self, name: str, timeout: int = 60) -> None:
        """终止容器（= 终止容器内全部进程树）。"""
        self.run(["stop", "-t", "10", name], timeout=timeout)

    def ensure_container_stopped(self, name: str, timeout: int = 60) -> None:
        """停止容器并**确认**其不再运行；必要时强制移除。

        超时清理（评审 P0-3 + 复核加固）：只调用 stop 不检查结果是不够的——运行中的
        超时进程可能继续修改挂载的任务目录；同样，inspect 查询失败不能冒充"容器不存在"。
        确认流程：stop（容忍已停止）→ inspect State.Running → 仍在运行则 rm -f →
        再次 inspect。任何无法确认的状态都以携带容器标识的错误抛出。
        """
        stop = self.run(["stop", "-t", "10", name], timeout=timeout)
        if stop.returncode != 0 and not self._benign_stop_error(stop.stderr):
            raise DockerCliError(
                f"docker stop failed for container '{name}': {stop.stderr.strip()[:200]}"
            )
        self._assert_not_running(name)

    @staticmethod
    def _benign_stop_error(stderr: str) -> bool:
        lowered = stderr.lower()
        # 容器已停止 / 已不存在：stop 非零属正常
        return "already stopped" in lowered or "no such container" in lowered

    def _inspect_running(self, name: str) -> bool | None:
        """查询容器运行状态。True/False = 确认运行中/已停止；None = 无法确认（查询失败）。"""
        result = self.run(
            ["container", "inspect", "-f", "{{.State.Running}}", name], timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip() == "true"
        # 非零退出必须区分"确实不存在"与"查询异常"（daemon 失联 / 权限等）——
        # 只有明确的 No such object 才能当作容器不存在
        if "no such object" in result.stderr.lower():
            return False
        return None

    def _assert_not_running(self, name: str, timeout: int = 60) -> None:
        running = self._inspect_running(name)
        if running is None:
            raise DockerCliError(
                f"CLEANUP UNVERIFIED: docker inspect failed for container '{name}' "
                "(daemon/query error); cannot confirm the container stopped"
            )
        if running is False:
            return  # 确认已停止（存在但未运行，或已移除）
        self.run(["rm", "-f", name], timeout=timeout)
        recheck = self._inspect_running(name)
        if recheck is True:
            raise DockerCliError(
                f"container '{name}' is STILL RUNNING after stop and forced removal"
            )
        if recheck is None:
            raise DockerCliError(
                f"CLEANUP UNVERIFIED: cannot confirm container '{name}' state after "
                "forced removal (docker inspect failed)"
            )
        # recheck is False：确认已移除或已停止

    def remove_container(self, name: str, timeout: int = 60) -> None:
        result = self.run(["rm", "-f", name], timeout=timeout)
        _ = result  # 容器不存在时 rm -f 返回非零：幂等容忍
