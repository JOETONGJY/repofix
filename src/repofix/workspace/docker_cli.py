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

    def remove_container(self, name: str, timeout: int = 60) -> None:
        result = self.run(["rm", "-f", name], timeout=timeout)
        _ = result  # 容器不存在时 rm -f 返回非零：幂等容忍
