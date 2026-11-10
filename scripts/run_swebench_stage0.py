"""SWE-bench Mini-Eval · Stage 0：零模型 Token 环境验证 + 选题冻结（评审修订版）。

对每个候选实例执行（全程无 DeepSeek 调用）：
  1. docker pull 官方实例镜像并记录 Digest（可追溯性）；
  2. docker create + cp 提取 /testbed（含已构建产物）；
  3. 可见测试计划推导：plan.py 冻结规则——只用 Issue 文本 + base 仓库结构，
     禁用 test_patch / F2P / P2P / gold patch（隐藏测试隔离，评审要求 1）；
  4. 工作区校验：HEAD == base_commit、工作区干净、可编辑安装指向 /testbed
     （评审要求 3）；
  5. rg 探测（确认缺失）→ 注入 → 复验可用；
  6. 可见基线运行 + 运行时预算判定（超 300s 的计划冻结前降级为空计划）；
  7. 判定器冒烟（无 Patch）：期望 F2P 全失败 + P2P 全通过；F2P 部分通过记为
     baseline_anomaly 并冻结淘汰（评审要求：先记录调查，不静默删除）；
  8. gold patch 校准：官方 gold patch（仅评测器持有）应被判 RESOLVED——
     验证环境 + 判定器全链路，绝少进入 Agent 输入（评审要求 2）。

产物：
  benchmarks/swebench-mini/selection.json      冻结名单（含全部淘汰原因）
  benchmarks/swebench-mini/stage0-report.json  逐步验证证据
  repofix-data/swebench/…                      数据集缓存 / rg / 提取目录

前置：Docker Desktop 运行中；Docker Hub 与 GitHub 经代理可达。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from repofix.evaluation.swebench import dataset, plan, selection
from repofix.evaluation.swebench.judge import extract_eval_commands, run_judge
from repofix.evaluation.swebench.workspace import (
    WorkspacePreparationError,
    build_agent_image,
    download_rg,
    force_rmtree,
    image_digest,
    run_install_commands,
    start_eval_container,
    top_level_package,
    verify_extracted_workspace,
)
from repofix.workspace.docker_cli import DockerCli

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "repofix-data" / "swebench"
FREEZE_DIR = REPO_ROOT / "benchmarks" / "swebench-mini"
FORMAL_TARGET = 10
BASELINE_BUDGET_SECONDS = 300


def _step(evidence: dict, name: str, ok: bool, detail: str = "", seconds: float = 0.0) -> bool:
    evidence["steps"].append(
        {"step": name, "ok": ok, "detail": detail[:300], "seconds": round(seconds, 1)})
    print(f"    [{'ok' if ok else 'BLOCKED'}] {name} ({seconds:.0f}s) {detail[:120]}")
    return ok


def _check_instance(cli: DockerCli, meta: dict, row: dict, rg_path: Path,
                    keep_testbeds: bool) -> dict:
    instance_id = meta["instance_id"]
    evidence = {"instance_id": instance_id, "steps": [], "ok": False}
    testbed = DATA_ROOT / "testbeds" / instance_id
    testbed.parent.mkdir(parents=True, exist_ok=True)
    container = f"sweb-preflight-{instance_id}"

    try:
        # 1. 镜像 + Digest
        t0 = time.time()
        if cli.image_exists(meta["image"]):
            _step(evidence, "image_present", True, "cached locally", time.time() - t0)
        else:
            print(f"    pulling {meta['image']} ...")
            pulled = cli.run(["pull", meta["image"]], timeout=3600)
            if not _step(evidence, "image_pull", pulled.returncode == 0,
                         pulled.stderr.strip()[-200:], time.time() - t0):
                return evidence
        digest = image_digest(cli, meta["image"])
        evidence["image_digest"] = digest

        # 2. 提取 /testbed
        t0 = time.time()
        force_rmtree(testbed)
        testbed.mkdir(parents=True)
        try:
            from repofix.evaluation.swebench.workspace import extract_testbed
            extract_testbed(cli, meta["image"], testbed, base_commit=meta["base_commit"])
        except WorkspacePreparationError as exc:
            _step(evidence, "testbed_extract", False, str(exc), time.time() - t0)
            return evidence
        _step(evidence, "testbed_extract", True,
              f"{sum(1 for _ in testbed.iterdir())} top entries", time.time() - t0)

        # 3. 可见测试计划（仅 Issue + 仓库结构；隐藏字段不参与）
        t0 = time.time()
        argv, plan_source, plan_detail = plan.derive_visible_plan(
            row["problem_statement"], testbed)
        plan_detail["argv"] = argv
        plan_detail["plan_source"] = plan_source
        evidence["visible_plan"] = plan_detail
        _step(evidence, "visible_plan_derive", True,
              f"source={plan_source} layer={plan_detail['layer']} "
              f"targets={plan_detail['targets']}", time.time() - t0)

        # 4-6. 派生镜像 + 容器 + 工作区校验 + 基线
        t0 = time.time()
        try:
            agent_image = build_agent_image(cli, meta["image"], instance_id, rg_path,
                                            pkg_version=str(row.get("version") or ""))
        except WorkspacePreparationError as exc:
            _step(evidence, "agent_image_build", False, str(exc), time.time() - t0)
            return evidence
        evidence["agent_image"] = agent_image
        _step(evidence, "agent_image_build", True, agent_image, time.time() - t0)

        t0 = time.time()
        try:
            start_eval_container(cli, container, testbed, agent_image)
        except WorkspacePreparationError as exc:
            _step(evidence, "container_start", False, str(exc), time.time() - t0)
            return evidence
        try:
            _step(evidence, "container_start", True, "", time.time() - t0)
            checks = verify_extracted_workspace(
                cli, container, testbed, meta["base_commit"],
                top_level_package(meta["gold_file"]))
            evidence["workspace_checks"] = checks
            # 阻塞项：base 一致 + 已跟踪文件干净 + rg 可用 + python 在 testbed 环境。
            # import 探针仅诊断（部分仓库不预装 site-packages，官方靠 eval_script 的
            # pip install -e / pytest rootdir 机制解析——判定器会重放官方 install 命令）
            env_ok = "envs/testbed" in checks.get("python_executable", "")
            ws_ok = checks["head_matches_base"] and checks["worktree_clean"] and \
                checks["rg_available"] and env_ok
            if not _step(evidence, "workspace_validation", ws_ok, json.dumps(checks, ensure_ascii=False)[:280]):
                return evidence

            # 4b. 官方 install 步骤（环境准备，非泄漏：来自 eval_script 的通用构建命令，
            # 与 Issue 无关；官方 harness 在评测前同样执行。禁网容器内 build isolation
            # 失败时自动以 --no-build-isolation --no-deps 重试，依赖由镜像预装备齐）
            installs, _tests = extract_eval_commands(row["eval_script"])
            t1 = time.time()
            install_evidence = run_install_commands(
                cli, container, installs,
                env_prefix=f"SETUPTOOLS_SCM_PRETEND_VERSION={row.get('version', '')} "
                if row.get("version") else "")
            evidence["official_install"] = install_evidence
            if not _step(evidence, "official_install", install_evidence["ok"],
                         json.dumps(install_evidence["attempts"], ensure_ascii=False)[:260],
                         time.time() - t1):
                return evidence

            if plan_detail.get("targets"):
                # 逐目标基线计时（机械、环境侧事实）：超预算或无可收集测试的目标剔除；
                # 决策发生在冻结前，全部留痕。rc=1（基线断言失败）是有效基线，保留。
                kept, timing = [], []
                for target in plan_detail["targets"]:
                    t1 = time.time()
                    single = plan.visible_plan_argv([target])
                    outcome = cli.exec(container, single, timeout=BASELINE_BUDGET_SECONDS + 60,
                                       cwd_in_container="/testbed")
                    duration = time.time() - t1
                    timing.append({"target": target, "rc": outcome.returncode,
                                   "seconds": round(duration, 1)})
                    if duration <= BASELINE_BUDGET_SECONDS and outcome.returncode in (0, 1):
                        kept.append(target)
                evidence["target_timing"] = timing
                if kept:
                    plan_detail["argv"] = plan.visible_plan_argv(kept)
                    plan_detail["kept_targets"] = kept
                    _step(evidence, "visible_baseline", True,
                          f"kept {len(kept)}/{len(timing)} targets: {kept}", sum(t["seconds"] for t in timing))
                else:
                    plan_detail["argv"] = []
                    plan_detail["plan_source"] = "empty_over_budget"
                    _step(evidence, "visible_baseline", True,
                          "no target within budget -> empty plan (INCONCLUSIVE finish)",
                          sum(t["seconds"] for t in timing))
            else:
                _step(evidence, "visible_baseline", True, "empty plan (skipped)")

            # 7. 判定器冒烟（无 Patch）：F2P 全失败 + P2P 全通过
            t2 = time.time()
            smoke = run_judge(
                cli, image=agent_image, base_commit=meta["base_commit"],
                test_patch=row["test_patch"], fail_to_pass=meta["fail_to_pass"],
                pass_to_pass=meta["pass_to_pass"], eval_script=row["eval_script"],
                patch_path=None, work_root=DATA_ROOT / "judge-calibration",
                instance_id=f"{instance_id}-smoke", keep_workspace=False,
                pkg_version=str(row.get("version") or ""),
            )
            f2p_failed = len(smoke.f2p_failures)
            anomaly = smoke.outcome == "FAILED" and 0 < f2p_failed < len(meta["fail_to_pass"])
            smoke_ok = (smoke.outcome == "FAILED"
                        and f2p_failed == len(meta["fail_to_pass"])
                        and not smoke.p2p_regressions)
            if anomaly:
                # 评审要求：基线异常先记录调查，不静默删除、不盲目归因
                _step(evidence, "judge_smoke", False,
                      f"BASELINE ANOMALY: {f2p_failed}/{len(meta['fail_to_pass'])} F2P already "
                      f"pass at base; investigate before including. {smoke.reason[:120]}",
                      time.time() - t2)
                evidence["smoke_tail"] = smoke.test_output_tail[-600:]
                return evidence
            if not _step(evidence, "judge_smoke", smoke_ok,
                         f"outcome={smoke.outcome} f2p_failed={f2p_failed}/"
                         f"{len(meta['fail_to_pass'])} p2p_regressed={len(smoke.p2p_regressions)} "
                         f"{smoke.reason[:120]}", time.time() - t2):
                evidence["smoke_tail"] = smoke.test_output_tail[-600:]
                return evidence

            # 8. gold patch 校准（gold 只进评测器，不进 Agent 输入）
            t3 = time.time()
            gold_path = DATA_ROOT / "judge-calibration" / f"{instance_id}-gold.patch"
            gold_path.parent.mkdir(parents=True, exist_ok=True)
            gold_path.write_text(row["patch"], encoding="utf-8", newline="\n")
            gold = run_judge(
                cli, image=agent_image, base_commit=meta["base_commit"],
                test_patch=row["test_patch"], fail_to_pass=meta["fail_to_pass"],
                pass_to_pass=meta["pass_to_pass"], eval_script=row["eval_script"],
                patch_path=gold_path, work_root=DATA_ROOT / "judge-calibration",
                instance_id=f"{instance_id}-gold", keep_workspace=False,
                pkg_version=str(row.get("version") or ""),
            )
            gold_path.unlink(missing_ok=True)
            evidence["gold_calibration"] = {
                "outcome": gold.outcome, "reason": gold.reason[:300],
                "f2p_results": gold.f2p_results,
                "p2p_regressions": gold.p2p_regressions[:10],
            }
            if not _step(evidence, "gold_calibration", gold.outcome == "RESOLVED",
                         f"outcome={gold.outcome} {gold.reason[:160]}", time.time() - t3):
                return evidence

            evidence["ok"] = True
            return evidence
        finally:
            cli.remove_container(container)
    except Exception as exc:  # noqa: BLE001 —— 单实例故障不中断整轮验证
        evidence["unexpected"] = f"{type(exc).__name__}: {exc}"
        return evidence
    finally:
        if not keep_testbeds:
            force_rmtree(testbed)


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench Mini-Eval stage 0 (no model tokens)")
    parser.add_argument("--target", type=int, default=FORMAL_TARGET)
    parser.add_argument("--proxy", default=None, help="HTTP proxy for rg download (e.g. http://127.0.0.1:7897)")
    parser.add_argument("--keep-testbeds", action="store_true", help="keep extracted testbed dirs")
    parser.add_argument("--only", nargs="*", default=None, help="restrict validation to these instance_ids")
    parser.add_argument("--refreeze", action="store_true",
                        help="skip validation; rebuild selection.json from the merged report")
    parser.add_argument("--from-selection", default=None,
                        help="validate a pre-frozen list (selection-40.json formal_40) instead of "
                             "select_candidates output; freeze to the same path + '-validated' suffix")
    args = parser.parse_args()

    cache = DATA_ROOT / "dataset"
    test_rows = dataset.load_split("test", cache)
    dev_rows = dataset.load_split("dev", cache)
    if args.from_selection:
        frozen = json.loads(Path(args.from_selection).read_text(encoding="utf-8"))
        by_id = {m["instance_id"]: m for m in frozen["formal_40"]}
        candidates = [by_id[r["instance_id"]] for r in test_rows if r["instance_id"] in by_id]
        missing = set(by_id) - {c["instance_id"] for c in candidates}
        if missing:
            print(f"[stage0] ERROR: frozen instances not in test split: {missing}", file=sys.stderr)
            return 2
    else:
        candidates = selection.select_candidates(test_rows, target=args.target)
    rehearsal_list = selection.select_candidates(dev_rows, target=3)
    if not candidates:
        print("[stage0] selection produced no candidates; aborting", file=sys.stderr)
        return 2
    # 预演题按冻结顺序回退：首个候选环境阻塞 → 取下一候选（模型运行前的环境
    # 淘汰，属协议允许的确定性回退；被环境淘汰者照常留痕）
    print(f"[stage0] formal candidates: {len(candidates)}, "
          f"rehearsal queue: {[m['instance_id'] for m in rehearsal_list]}")

    rg_path = download_rg(DATA_ROOT / "tools", proxy=args.proxy)
    print(f"[stage0] rg binary ready: {rg_path} ({rg_path.stat().st_size // 1024} KB)")

    cli = DockerCli()
    # 报告合并：增量重验（--only）时保留已有实例的验证结果，仅覆盖本次条目
    # from-selection 模式：独立的报告与冻结文件（不覆盖 8 题 v1.0 的历史证据）
    if args.from_selection:
        sel_path = Path(args.from_selection)
        report_path = sel_path.parent / (sel_path.stem + "-stage0-report.json")
        freeze_path = sel_path.parent / (sel_path.stem + "-validated.json")
    else:
        report_path = FREEZE_DIR / "stage0-report.json"
        freeze_path = FREEZE_DIR / "selection.json"
    prior_path = report_path
    prior_instances = []
    if prior_path.exists():
        try:
            prior_instances = json.loads(prior_path.read_text(encoding="utf-8")).get("instances", [])
        except (OSError, ValueError):
            prior_instances = []
    report = {
        "started_at": time.time(), "dataset_commit": _dataset_commit(),
        "rules_version": selection.SELECTION_RULES_VERSION,
        "plan_rules_version": plan.__doc__.split("visible_plan_rule_v1")[0].strip()[-40:] + "visible_plan_rule_v1",
        "funnel_test": selection.funnel(test_rows),
        "funnel_dev": selection.funnel(dev_rows),
        "instances": list(prior_instances),  # 合并历史结果：todo 构建与冻结都能看到
    }
    todo = [(m, "formal") for m in candidates]
    for m in rehearsal_list:
        todo.append((m, "rehearsal"))
        if args.only is None and _ok(report, m["instance_id"]):
            break  # 已拿到环境有效的预演题（含合并报告中的历史结果），停止按序验证
    if args.refreeze:
        # 从合并报告重建完整冻结名单：formal 全量 + 预演队列全部计入淘汰统计
        todo = [(m, "formal") for m in candidates] + [(m, "rehearsal") for m in rehearsal_list]
    elif args.only:
        todo = [(m, tag) for m, tag in todo if m["instance_id"] in set(args.only)]
    for meta, tag in (todo if not args.refreeze else []):
        row = next(r for r in (test_rows + dev_rows) if r["instance_id"] == meta["instance_id"])
        print(f"[stage0] validating {tag}: {meta['instance_id']}")
        evidence = _check_instance(cli, meta, row, rg_path, args.keep_testbeds)
        evidence["role"] = tag
        # 覆盖式写入：同实例以本次验证结果为准
        report["instances"] = [e for e in report["instances"]
                               if e["instance_id"] != meta["instance_id"]]
        report["instances"].append(evidence)
        print(f"[stage0] -> {'ELIGIBLE' if evidence.get('ok') else 'ENVIRONMENT_BLOCKED'}\n")
        # 逐步落盘：中断也有证据
        report["finished_at"] = time.time()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    eligible_formal = [m for m in candidates if _ok(report, m["instance_id"])]
    eligible_rehearsal = next(
        (m for m in rehearsal_list if _ok(report, m["instance_id"])), None)
    blocked = [
        {"instance_id": m["instance_id"], "role": tag,
         "reason": _block_reason(report, m["instance_id"])}
        for m, tag in todo if not _ok(report, m["instance_id"])
    ]
    out = selection.write_selection_json(
        freeze_path, eligible_formal, eligible_rehearsal, blocked,
        report["dataset_commit"])
    payload = json.loads(out.read_text(encoding="utf-8"))
    print("=" * 60)
    print(f"frozen selection: {out}")
    print(f"  formal eligible : {len(payload['formal'])}/{args.target}")
    for m in payload["formal"]:
        print(f"    {m['instance_id']}")
    print(f"  rehearsal       : {payload['rehearsal']['instance_id'] if payload['rehearsal'] else 'NONE'}")
    for b in payload["environment_blocked"]:
        print(f"  BLOCKED {b['instance_id']}: {b['reason'][:100]}")
    print("NO model tokens were spent in stage 0.")
    return 0


def _ok(report: dict, instance_id: str) -> bool:
    return any(e["instance_id"] == instance_id and e.get("ok") for e in report["instances"])


def _block_reason(report: dict, instance_id: str) -> str:
    for e in report["instances"]:
        if e["instance_id"] == instance_id:
            failed = [s for s in e.get("steps", []) if not s["ok"]]
            return failed[0]["detail"] if failed else e.get("unexpected", "unknown")
    return "not validated"


def _dataset_commit() -> str:
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://hf-mirror.com/api/datasets/SWE-bench/SWE-bench_Lite",
            headers={"User-Agent": "repofix-mini-eval"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("sha", "unknown")
    except Exception:  # noqa: BLE001 —— 版本号获取失败不阻塞验证
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
