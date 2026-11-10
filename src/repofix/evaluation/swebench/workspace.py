"""SWE-bench 工作区适配层：官方 per-instance 镜像 ↔ RepoFix Workspace 契约。

设计要点（stage-0 验证过的假设，见 docs/BENCHMARK-MINI.md）：
- 环境 = 官方实例镜像（数据集 image 字段），不使用 repofix-env——历史仓库的
  Python 版本与依赖只有官方镜像保证（可编辑安装指向 /testbed）。
- 工作区提取：`docker create`（不启动）+ `docker cp /testbed/.` 到宿主目录。
  提取的是镜像内**已构建**的仓库状态（含 C 扩展 .so 等原地产物），RepoFix 的
  宿主侧文件工具（read_file / apply_patch / get_changes）直接可用；
  命令执行时把该目录 bind-mount 回 /testbed，依赖解析与官方环境完全一致。
  替代方案（挂载干净 clone + 评测前重编译）被否决：需要 per-repo 构建命令且
  可能偏离官方镜像状态。
- 容器以 root 运行（对 RepoFix repofix-env 的非 root 约束是一次显式偏离）：
  官方 conda 环境属 root，非 root 会破坏环境一致性；评测容器无网络、
  限资源，权限模型与官方 harness 相同。
- ripgrep：官方镜像无 rg（search_code 会整体失效）。不构建派生镜像——
  运行时 `docker cp` 一个静态 musl rg 二进制进容器并 chmod，等价且更快。
"""
import subprocess
import uuid
from pathlib import Path

from repofix.workspace.docker_cli import DockerCli

SWEBENCH_MOUNT = "/testbed"
RG_RELEASE_URL = (
    "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/"
    "ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz"
)


class WorkspacePreparationError(RuntimeError):
    """官方工作区提取或容器启动失败（stage-0 记 environment_blocked）。"""


def _extract_tarball_regular(tarball: Path, dest_dir: Path) -> None:
    """解包 tarball 中的普通文件与目录；符号链接成员跳过，交给 git 归一化。

    提取后由 extract_testbed 设 `core.symlinks=false` 并 `git checkout -f` 重建
    ——这是 git 在 Windows 上的原生语义（symlink blob 落盘为含链接目标文本的
    普通文件）：工作区状态干净、diff 无伪影；若仓库测试依赖 symlink 行为，
    由判定器冒烟与 gold 校准实证裁决。
    """
    import tarfile

    with tarfile.open(tarball) as tf:
        members = [m for m in tf.getmembers() if m.isreg() or m.isdir()]
        tf.extractall(dest_dir, members=members, filter="data")


def force_rmtree(path: Path) -> None:
    """删除目录树（Windows：docker cp 提取的 git 对象带只读位，chmod 后重试）。

    与 WorkspaceManager._rmtree 同款策略；评测侧多处清理共用。
    """
    import os
    import shutil
    import stat

    path = Path(path)
    if not path.exists():
        return

    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def extract_testbed(cli: DockerCli, image: str, dest_dir: Path,
                    base_commit: str | None = None, timeout: int = 1800) -> dict:
    """从官方实例镜像提取 /testbed 到宿主目录（含 .git 与已构建产物）。

    提取机制（stage-0 修订）：容器内 `tar -h` 解引用后打成单文件 tarball 经
    docker cp 传出——matplotlib 等镜像内的 symlink（vendored freetype）会让
    docker cp 在 Windows 上直接失败；tar -h 把符号链接物化为普通文件，宿主
    无需 symlink 权限。返回提取证据（tar 警告等）。

    base_commit：官方镜像的 /testbed 停在 environment_setup_commit（装依赖的
    版本），官方 harness 在运行时才 checkout base——本函数在提取后补
    `git checkout --detach <base_commit>`，与 WorkspaceManager 的快照语义一致。
    """
    import subprocess

    dest_dir = Path(dest_dir)
    if any(dest_dir.iterdir()):
        raise WorkspacePreparationError(f"destination not empty: {dest_dir}")
    name = f"sweb-extract-{uuid.uuid4().hex[:10]}"
    started = cli.run(
        ["run", "-d", "--name", name, "--network", "none", image, "sleep", "infinity"],
        timeout=180,
    )
    if started.returncode != 0:
        raise WorkspacePreparationError(f"docker run (extract) failed: {started.stderr.strip()[:200]}")
    evidence: dict = {"symlink_note": ""}
    try:
        # cwd 用 /：提取容器未挂载卷，DockerCli.exec 默认的 /workspace 不存在。
        # 不用 -h：符号链接成员保留在 tar 里，宿主侧尽量还原真 symlink
        #（tar -h 解引用会把仓库内 symlink 文件物化成内容副本，污染已跟踪文件状态）
        packed = cli.exec(name, ["tar", "-C", SWEBENCH_MOUNT, "-cf", "/tmp/testbed.tar", "."],
                          timeout=timeout, cwd_in_container="/")
        if packed.returncode != 0:
            evidence["symlink_note"] = packed.stderr.strip()[-200:]
        tarball = dest_dir / ".testbed.tar"
        copied = cli.run(["cp", f"{name}:/tmp/testbed.tar", str(tarball)], timeout=timeout)
        if copied.returncode != 0:
            raise WorkspacePreparationError(
                f"docker cp tarball failed: {copied.stderr.strip()[:200]}; "
                f"tar stderr: {evidence['symlink_note'][:200]}")
        if tarball.stat().st_size < 1024 * 1024:
            raise WorkspacePreparationError(f"tarball suspiciously small: {tarball.stat().st_size} bytes")
        _extract_tarball_regular(tarball, dest_dir)
        tarball.unlink(missing_ok=True)
    finally:
        cli.run(["rm", "-f", name], timeout=120)
    if not (dest_dir / ".git").exists():
        raise WorkspacePreparationError("extracted testbed has no .git; unexpected image layout")
    if base_commit:
        # core.symlinks=false：git 的 Windows 原生语义——symlink blob 以含链接
        # 目标文本的普通文件落盘（tar 跳过的 symlink 成员由此重建），工作区
        # 状态与 diff 无提取伪影；行为差异由 smoke + gold 实证裁决
        subprocess.run(["git", "config", "core.symlinks", "false"],
                       cwd=str(dest_dir), capture_output=True, timeout=60, check=True)
        # core.fileMode=false：Windows FS 丢失可执行位，否则 Agent 的 get_changes/
        # export_patch 会混入 "old mode/new mode" 漂移 hunk，污染候选 Patch
        subprocess.run(["git", "config", "core.fileMode", "false"],
                       cwd=str(dest_dir), capture_output=True, timeout=60, check=True)
        checkout = subprocess.run(
            ["git", "-c", "core.fileMode=false", "checkout", "--quiet", "--detach", base_commit],
            cwd=str(dest_dir), capture_output=True, text=True, timeout=900,
        )
        if checkout.returncode != 0:
            raise WorkspacePreparationError(
                f"git checkout {base_commit[:12]} failed: {(checkout.stderr or '').strip()[:200]}")
        normalize = subprocess.run(
            ["git", "-c", "core.fileMode=false", "checkout", "-f", "--", "."],
            cwd=str(dest_dir), capture_output=True, text=True, timeout=900,
        )
        if normalize.returncode != 0:
            raise WorkspacePreparationError(
                f"worktree normalize failed: {(normalize.stderr or '').strip()[:200]}")
    return evidence


def start_eval_container(cli: DockerCli, name: str, task_dir: Path, image: str,
                         *, memory: str = "4g", cpus: str = "2.0", pids_limit: int = 512) -> None:
    """启动评测容器：无网络 / 限资源 / 仅挂载任务副本（root，理由见模块 docstring）。"""
    cli.remove_container(name)
    result = cli.run(
        [
            "run", "-d", "--name", name,
            "--network", "none",
            "--memory", memory,
            "--cpus", cpus,
            "--pids-limit", str(pids_limit),
            "--security-opt", "no-new-privileges",
            "--volume", f"{Path(task_dir).resolve()}:{SWEBENCH_MOUNT}",
            "--workdir", SWEBENCH_MOUNT,
            image,
            "sleep", "infinity",
        ],
        timeout=180,
    )
    if result.returncode != 0:
        raise WorkspacePreparationError(
            f"container start failed for {name}: {result.stderr.strip()[:200]}")


def download_rg(cache_dir: Path, proxy: str | None = None, timeout: int = 120) -> Path:
    """下载静态 musl rg 二进制（宿主执行，不入容器构建）。返回可执行文件路径。"""
    import io
    import tarfile
    import urllib.request

    cache_dir = Path(cache_dir)
    cached = cache_dir / "rg"
    if cached.exists() and cached.stat().st_size > 0:
        return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(RG_RELEASE_URL, headers={"User-Agent": "repofix-mini-eval"})
    with opener.open(req, timeout=timeout) as resp:
        payload = resp.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith("/rg"))
        extracted = tar.extractfile(member)
        if extracted is None:
            raise WorkspacePreparationError("rg binary not found in release tarball")
        cached.write_bytes(extracted.read())
    return cached


def install_rg(cli: DockerCli, container: str, rg_host_path: Path) -> dict:
    """运行时注入 rg（保留给未走派生镜像的路径）：cp + chmod + 探测验证。"""
    probe_before = cli.exec(container, ["sh", "-c", "command -v rg || echo ABSENT"], timeout=30)
    rg_was_absent = "ABSENT" in probe_before.stdout
    copied = cli.run(["cp", str(rg_host_path), f"{container}:/usr/local/bin/rg"], timeout=60)
    if copied.returncode != 0:
        raise WorkspacePreparationError(f"docker cp rg failed: {copied.stderr.strip()[:200]}")
    chmod = cli.exec(container, ["chmod", "+x", "/usr/local/bin/rg"], timeout=30)
    if chmod.returncode != 0:
        raise WorkspacePreparationError(f"chmod rg failed: {chmod.stderr.strip()[:200]}")
    probe_after = cli.exec(container, ["rg", "--version"], timeout=30)
    if probe_after.returncode != 0:
        raise WorkspacePreparationError(f"rg probe failed in container: {probe_after.stderr.strip()[:200]}")
    return {"rg_was_absent": rg_was_absent,
            "rg_version": probe_after.stdout.strip().splitlines()[0][:60]}


def probe_python_env(cli: DockerCli, container: str, expected_package: str | None) -> dict:
    """探测容器内 python/rg 环境（派生镜像生效证据）。所有 exec 显式指定 cwd。"""
    py = cli.exec(container, ["python", "-c", "import sys; print(sys.executable)"],
                  timeout=60, cwd_in_container=SWEBENCH_MOUNT)
    rg = cli.exec(container, ["rg", "--version"], timeout=30, cwd_in_container=SWEBENCH_MOUNT)
    import_ok = None
    import_detail = ""
    if expected_package and py.returncode == 0:
        probe = cli.exec(
            container,
            ["python", "-c",
             f"import {expected_package} as m; print(list(m.__path__)[0] if hasattr(m, '__path__') else m.__file__)"],
            timeout=120, cwd_in_container=SWEBENCH_MOUNT,
        )
        import_ok = probe.returncode == 0
        import_detail = (probe.stdout.strip() or probe.stderr.strip())[:200]
        if import_ok:
            import_ok = import_detail.startswith("/testbed")
    return {
        "python_executable": py.stdout.strip()[:80] if py.returncode == 0 else f"rc={py.returncode}",
        "rg_available": rg.returncode == 0,
        "import_points_to_testbed": import_ok,
        "import_path": import_detail,
        "package_probed": expected_package,
    }


DERIVED_IMAGE_PREFIX = "repofix-swebenv"


def run_install_commands(cli: DockerCli, container: str, installs: list[str],
                         env_prefix: str = "", timeout: int = 900) -> dict:
    """在容器内执行官方 eval_script 的 install 命令（环境准备，非泄漏）。

    官方容器有网络、我们的评测容器禁网：`pip install -e .` 的 build isolation
    需要联网拉构建依赖。策略：先按官方原样执行；失败则追加
    `--no-build-isolation --no-deps` 重试一次（依赖已由官方镜像构建时备齐），
    两次结果都留痕。全部失败由调用方决定处置（stage-0 记 blocked）。
    """
    evidence: list[dict] = []
    ok = True
    for command in installs:
        # env_prefix：如 SETUPTOOLS_SCM_PRETEND_VERSION=<ver>——在 Docker Desktop 的
        # Windows 挂载上，setuptools_scm 的 git describe（dirty 全树遍历）会慢到
        # 超时/挂起（stage-0 实测 619s，并行时 >900s 失败）；数据集 version 字段
        # 提供等价版本号，跳过 git 遍历。对不使用 setuptools_scm 的仓库无副作用。
        result = cli.run(["exec", "--workdir", SWEBENCH_MOUNT, container, "bash", "-c",
                          f"{env_prefix}{command}" if env_prefix else command],
                         timeout=900)
        record = {"command": command, "rc": result.returncode, "mode": "official"}
        if result.returncode != 0:
            fallback = f"{command} --no-build-isolation --no-deps"
            retry = cli.run(["exec", "--workdir", SWEBENCH_MOUNT, container, "bash", "-c", fallback],
                            timeout=timeout)
            record["fallback_rc"] = retry.returncode
            record["fallback_command"] = fallback
            record["tail"] = (retry.stdout + retry.stderr)[-300:]
            if retry.returncode != 0:
                ok = False
        else:
            record["tail"] = (result.stdout + result.stderr)[-200:]
        evidence.append(record)
    return {"ok": ok and bool(installs), "attempts": evidence}


def build_agent_image(cli: DockerCli, official_image: str, instance_id: str,
                      rg_host_path: Path, pkg_version: str = "") -> str:
    """派生 Agent/评测镜像（评审建议采纳，替代运行时注入）。

    官方镜像 PATH 只含 base conda（eval_script 靠显式 conda activate testbed），
    RepoFix 的 exec 链路（run_command / rg / 判定安装与测试命令）没有激活步骤——
    派生一层 ENV PATH=/opt/miniconda3/envs/testbed/bin:<原 PATH> 并 COPY 静态 rg，
    一次构建（离线，FROM 本地镜像），所有容器统一使用，按实例缓存。
    """
    import shutil
    import tempfile

    tag = f"{DERIVED_IMAGE_PREFIX}:v2-{instance_id}"
    if cli.image_exists(tag):
        return tag
    inspect = cli.run(
        ["image", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", official_image],
        timeout=30,
    )
    base_path = ""
    for line in inspect.stdout.splitlines():
        if line.startswith("PATH="):
            base_path = line[len("PATH="):].strip()
            break
    env_bin = "/opt/miniconda3/envs/testbed/bin"
    new_path = f"{env_bin}:{base_path}" if base_path else env_bin
    context = Path(tempfile.mkdtemp(prefix="sweb-img-"))
    try:
        (context / "Dockerfile").write_text(
            f"FROM {official_image}\nENV PATH={new_path}\nCOPY rg /usr/local/bin/rg\n",
            encoding="utf-8", newline="\n",
        )
        shutil.copy(Path(rg_host_path), context / "rg")
        built = cli.run(["build", "-t", tag, str(context)], timeout=900)
        if built.returncode != 0:
            raise WorkspacePreparationError(
                f"agent image build failed for {instance_id}: {built.stderr.strip()[-300:]}")
    finally:
        force_rmtree(context)
    return tag


def verify_extracted_workspace(cli: DockerCli, container: str, testbed: Path,
                               base_commit: str, expected_package: str | None) -> dict:
    """工作区校验（评审要求 3）：base 一致、工作区干净、可编辑安装指向 /testbed。"""
    import subprocess

    def host_git(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", "core.fileMode=false", *args], cwd=str(testbed),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )

    head = host_git(["rev-parse", "HEAD"])
    # 工作区干净 = 无**已跟踪文件**改动（镜像构建期产生的未跟踪 .so 等原生
    # 产物属预期状态，由 .gitignore 覆盖；隐藏测试泄漏只会体现为跟踪文件改动）
    status_tracked = host_git(["status", "--porcelain", "--untracked-files=no"])
    status_all = host_git(["status", "--porcelain"])
    head_ok = head.returncode == 0 and head.stdout.strip() == base_commit
    clean_ok = status_tracked.returncode == 0 and not status_tracked.stdout.strip()

    env_probe = probe_python_env(cli, container, expected_package)
    return {
        "head_matches_base": head_ok,
        "head": head.stdout.strip()[:12],
        "worktree_clean": clean_ok,
        "untracked_entries": len([ln for ln in status_all.stdout.splitlines() if ln.strip()]),
        **env_probe,
    }


def image_digest(cli: DockerCli, image: str) -> str:
    """记录镜像 Digest（可追溯性，评审要求 3/报告）。"""
    result = cli.run(["image", "inspect", "--format", "{{index .RepoDigests 0}}", image], timeout=30)
    return result.stdout.strip() if result.returncode == 0 else ""


def top_level_package(gold_file: str) -> str | None:
    """gold patch 文件路径 → 顶层 import 包名（仅用于环境校验，不给 Agent）。"""
    parts = [p for p in Path(gold_file).parts if p not in ("src", "lib")]
    return parts[0] if parts and not parts[0].endswith(".py") else None


def visible_test_plan_removed() -> None:
    """占位说明：test_patch 反推可见计划的做法已删除（GPT 评审要求 1：隐藏测试隔离）。

    可见测试计划改由 plan.py 的冻结规则推导（仅用 Issue 文本 + base 仓库结构）。
    test_patch / F2P / P2P / gold patch 只供评测器与 stage-0 环境验收使用。
    """


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
