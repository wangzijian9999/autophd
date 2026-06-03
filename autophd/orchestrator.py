#!/usr/bin/env python3
"""Closed-loop automated research orchestrator.

This tool coordinates:
idea discovery -> Codex implementation -> sanity checks -> training ->
evaluation -> Codex/Claude reflection -> metric gate -> rollback/promotion.

It is intentionally adapter-based. External projects such as AI-Scientist,
OpenScholar, PaperQA, RD-Agent, or custom training scripts are invoked through
configured shell commands instead of being vendored into this repository.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


class AutoResearchError(RuntimeError):
    """Raised when a research loop invariant is violated."""


@dataclass
class CommandResult:
    name: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclass
class GateDecision:
    promote: bool
    reasons: List[str]
    metric_summary: Dict[str, Any]


def utc_timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def load_config(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise AutoResearchError(f"Config file does not exist: {config_path}")

    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise AutoResearchError(
                "PyYAML is required for YAML config files. "
                "Install pyyaml or use a JSON config."
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise AutoResearchError("Config root must be an object.")
    return data


def get_config_value(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = config
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def require_config_value(config: Dict[str, Any], path: str) -> Any:
    value = get_config_value(config, path)
    if value is None:
        raise AutoResearchError(f"Missing required config value: {path}")
    return value


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(text)


def run_subprocess(
    command: Sequence[str],
    cwd: Path,
    input_text: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    check: bool = False,
) -> CommandResult:
    start_time = time.time()
    completed_process = subprocess.run(
        list(command),
        cwd=str(cwd),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed_seconds = time.time() - start_time
    result = CommandResult(
        name=Path(command[0]).name,
        command=" ".join(shlex.quote(part) for part in command),
        returncode=completed_process.returncode,
        stdout=completed_process.stdout,
        stderr=completed_process.stderr,
        elapsed_seconds=elapsed_seconds,
    )
    if check and result.returncode != 0:
        raise AutoResearchError(
            f"Command failed with exit code {result.returncode}: {result.command}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def render_template(template: str, values: Dict[str, Any]) -> str:
    escaped_values = {key: str(value) for key, value in values.items()}
    return template.format(**escaped_values)


def run_shell_template(
    name: str,
    command_template: str,
    values: Dict[str, Any],
    cwd: Path,
    log_dir: Path,
    input_text: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    check: bool = False,
) -> CommandResult:
    command = render_template(command_template, values)
    start_time = time.time()
    completed_process = subprocess.run(
        command,
        cwd=str(cwd),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        shell=True,
        check=False,
        executable="/bin/bash",
    )
    elapsed_seconds = time.time() - start_time
    result = CommandResult(
        name=name,
        command=command,
        returncode=completed_process.returncode,
        stdout=completed_process.stdout,
        stderr=completed_process.stderr,
        elapsed_seconds=elapsed_seconds,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    (log_dir / f"{safe_name}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / f"{safe_name}.stderr.log").write_text(result.stderr, encoding="utf-8")
    write_json(
        log_dir / f"{safe_name}.meta.json",
        {
            "name": name,
            "command": command,
            "returncode": result.returncode,
            "elapsed_seconds": result.elapsed_seconds,
        },
    )
    if check and result.returncode != 0:
        raise AutoResearchError(
            f"{name} failed with exit code {result.returncode}. "
            f"See logs under {log_dir}"
        )
    return result


def git(project_root: Path, args: Sequence[str], check: bool = True) -> CommandResult:
    return run_subprocess(["git"] + list(args), cwd=project_root, check=check)


def git_toplevel(project_root: Path) -> Path:
    result = git(project_root, ["rev-parse", "--show-toplevel"])
    return Path(result.stdout.strip()).resolve()


def git_head(project_root: Path) -> str:
    return git(project_root, ["rev-parse", "HEAD"]).stdout.strip()


def git_branch(project_root: Path) -> str:
    return git(project_root, ["branch", "--show-current"]).stdout.strip()


def relative_status_path(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def is_ignored_status_path(relative_path: str, ignored_paths: Iterable[str]) -> bool:
    normalized_path = relative_path.strip().lstrip("./")
    for ignored_path in ignored_paths:
        normalized_ignored = ignored_path.strip().lstrip("./").rstrip("/")
        if normalized_path == normalized_ignored or normalized_path.startswith(normalized_ignored + "/"):
            return True
    return False


def git_status_porcelain(
    project_root: Path,
    ignored_paths: Optional[Iterable[str]] = None,
) -> List[str]:
    result = git(project_root, ["status", "--porcelain"], check=True)
    ignored_paths = ignored_paths or []
    filtered_lines: List[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if is_ignored_status_path(parse_status_path(line), ignored_paths):
            continue
        filtered_lines.append(line)
    return filtered_lines


def parse_status_path(status_line: str) -> str:
    raw_path = status_line[3:]
    if " -> " in raw_path:
        raw_path = raw_path.split(" -> ", 1)[1]
    return raw_path.strip().strip('"')


def changed_paths(
    project_root: Path,
    ignored_paths: Optional[Iterable[str]] = None,
) -> List[str]:
    return [
        parse_status_path(line)
        for line in git_status_porcelain(project_root, ignored_paths=ignored_paths)
    ]


def is_path_allowed(relative_path: str, allowed_paths: Iterable[str]) -> bool:
    normalized_path = relative_path.strip().lstrip("./")
    for allowed_path in allowed_paths:
        normalized_allowed = allowed_path.strip().lstrip("./").rstrip("/")
        if normalized_path == normalized_allowed or normalized_path.startswith(normalized_allowed + "/"):
            return True
    return False


def ensure_allowed_changes(
    project_root: Path,
    allowed_paths: Sequence[str],
    ignored_paths: Sequence[str],
) -> List[str]:
    paths = changed_paths(project_root, ignored_paths=ignored_paths)
    disallowed_paths = [
        path for path in paths if not is_path_allowed(path, allowed_paths)
    ]
    if disallowed_paths:
        raise AutoResearchError(
            "Candidate changed files outside the allowlist:\n"
            + "\n".join(f"- {path}" for path in disallowed_paths)
        )
    return paths


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def numeric_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def compare_primary_metric(
    candidate_value: float,
    parent_value: Optional[float],
    mode: str,
    min_delta: float,
    allow_first_parent: bool,
) -> Tuple[bool, str]:
    if parent_value is None:
        if allow_first_parent:
            return True, "no parent metric exists; first verified parent is allowed"
        return False, "no parent metric exists and allow_first_parent is false"

    if mode == "maximize":
        delta = candidate_value - parent_value
        return delta >= min_delta, f"primary delta={delta:.6g}, required >= {min_delta:.6g}"
    if mode == "minimize":
        delta = parent_value - candidate_value
        return delta >= min_delta, f"primary improvement={delta:.6g}, required >= {min_delta:.6g}"
    raise AutoResearchError(f"Unsupported primary metric mode: {mode}")


def load_latest_parent_metric(state_dir: Path, primary_metric_name: str) -> Optional[float]:
    parent_records = read_jsonl(state_dir / "parents.jsonl")
    for record in reversed(parent_records):
        if record.get("status") != "promoted":
            continue
        metrics = record.get("metrics", {})
        metric_value = numeric_value(metrics.get(primary_metric_name))
        if metric_value is not None:
            return metric_value
    return None


def source_text_for_metrics(run_dir: Path, source: str) -> str:
    built_in_sources = {
        "idea_stdout": run_dir / "logs" / "idea.stdout.log",
        "idea_stderr": run_dir / "logs" / "idea.stderr.log",
        "train_stdout": run_dir / "logs" / "train.stdout.log",
        "train_stderr": run_dir / "logs" / "train.stderr.log",
        "evaluate_stdout": run_dir / "logs" / "evaluate.stdout.log",
        "evaluate_stderr": run_dir / "logs" / "evaluate.stderr.log",
    }
    source_path = built_in_sources.get(source, Path(source))
    if not source_path.is_absolute():
        source_path = run_dir / source_path
    if not source_path.exists():
        return ""
    return source_path.read_text(encoding="utf-8", errors="replace")


def collect_metrics(config: Dict[str, Any], run_dir: Path, template_values: Dict[str, Any]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}

    metrics_json_template = get_config_value(config, "metrics.metrics_json")
    if metrics_json_template:
        metrics_json_path = Path(render_template(metrics_json_template, template_values))
        if not metrics_json_path.is_absolute():
            metrics_json_path = run_dir / metrics_json_path
        if metrics_json_path.exists():
            metrics.update(json.loads(metrics_json_path.read_text(encoding="utf-8")))

    for extractor in get_config_value(config, "metrics.extractors", []):
        metric_name = extractor["name"]
        source = extractor.get("source", "evaluate_stdout")
        pattern = extractor["regex"]
        source_text = source_text_for_metrics(run_dir, source)
        match = re.search(pattern, source_text, flags=re.MULTILINE)
        if not match:
            continue
        raw_value = match.group(1)
        value_type = extractor.get("type", "float")
        if value_type == "int":
            metrics[metric_name] = int(raw_value)
        elif value_type == "str":
            metrics[metric_name] = raw_value
        else:
            metrics[metric_name] = float(raw_value)
    return metrics


def summarize_diff(project_root: Path, max_chars: int = 20000) -> str:
    result = git(project_root, ["diff", "--", "."], check=True)
    diff_text = result.stdout
    if len(diff_text) <= max_chars:
        return diff_text
    return diff_text[:max_chars] + "\n...[diff truncated]...\n"


class AutoResearchOrchestrator:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.config = load_config(self.config_path)
        configured_root = Path(get_config_value(self.config, "project.root", "."))
        if not configured_root.is_absolute():
            configured_root = (self.config_path.parent / configured_root).resolve()
        self.project_root = git_toplevel(configured_root)
        self.state_dir = self.project_root / get_config_value(
            self.config, "state.dir", ".auto_research"
        )
        self.ignored_status_paths = [relative_status_path(self.project_root, self.state_dir)]
        self.ignored_status_paths.extend(get_config_value(self.config, "git.ignore_status_paths", []))

    def template_values(self, run_id: str, run_dir: Path) -> Dict[str, Any]:
        return {
            "config_path": self.config_path,
            "project_root": self.project_root,
            "state_dir": self.state_dir,
            "run_id": run_id,
            "run_dir": run_dir,
            "prompt_file": run_dir / "prompt.txt",
        }

    def validate_config(self) -> None:
        require_config_value(self.config, "project.objective")
        require_config_value(self.config, "metrics.primary.name")
        require_config_value(self.config, "metrics.primary.mode")
        require_config_value(self.config, "commands.train")
        require_config_value(self.config, "commands.evaluate")
        allowed_paths = get_config_value(self.config, "files.allowed_paths", [])
        if not allowed_paths:
            raise AutoResearchError("files.allowed_paths must not be empty.")

    def init_state(self) -> None:
        self.validate_config()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for filename in ["experiments.jsonl", "parents.jsonl"]:
            path = self.state_dir / filename
            if not path.exists():
                path.write_text("", encoding="utf-8")
        dead_ends_path = self.state_dir / "DEAD_ENDS.md"
        if not dead_ends_path.exists():
            dead_ends_path.write_text("# Dead Ends\n\n", encoding="utf-8")
        insights_path = self.state_dir / "INSIGHTS.md"
        if not insights_path.exists():
            insights_path.write_text("# Verified Insights\n\n", encoding="utf-8")
        protocol_path = self.state_dir / "PROTOCOL.md"
        if not protocol_path.exists():
            protocol_path.write_text(self.protocol_text(), encoding="utf-8")

    def protocol_text(self) -> str:
        return (
            "# Automated Research Protocol\n\n"
            "1. Every candidate starts from the current verified parent.\n"
            "2. Failed or degraded candidates are diagnostic only and must not become parents.\n"
            "3. Promotion requires the configured primary metric gate, non-regression gates, "
            "mechanism evidence gates, and available cross-agent review records.\n"
            "4. Evidence gaps must be recorded as insufficient information, uncertain, or unknown.\n"
            "5. No result, citation, SOTA claim, or experimental outcome may be fabricated.\n"
        )

    def run_once(self) -> GateDecision:
        self.init_state()
        run_id = utc_timestamp()
        run_dir = self.state_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        template_values = self.template_values(run_id, run_dir)

        require_clean_worktree = bool(get_config_value(self.config, "git.require_clean_worktree", True))
        if require_clean_worktree and git_status_porcelain(
            self.project_root,
            ignored_paths=self.ignored_status_paths,
        ):
            raise AutoResearchError(
                "Working tree is not clean. Commit/stash unrelated changes before running, "
                "or set git.require_clean_worktree=false after reviewing the risk."
            )

        parent_branch = git_branch(self.project_root)
        parent_commit = git_head(self.project_root)
        candidate_branch = self.create_candidate_branch(run_id)
        run_record: Dict[str, Any] = {
            "run_id": run_id,
            "started_at": utc_timestamp(),
            "parent_branch": parent_branch,
            "parent_commit": parent_commit,
            "candidate_branch": candidate_branch,
            "status": "running",
        }
        write_json(run_dir / "run_record.json", run_record)

        try:
            idea_text = self.run_idea_stage(run_dir, template_values)
            self.run_codex_patch_stage(run_dir, template_values, idea_text)
            allowed_paths = get_config_value(self.config, "files.allowed_paths", [])
            candidate_changed_paths = ensure_allowed_changes(
                self.project_root,
                allowed_paths,
                self.ignored_status_paths,
            )
            self.run_command_sequence("sanity", run_dir, template_values, check=True)
            self.run_required_command("train", run_dir, template_values, check=True)
            self.run_required_command("evaluate", run_dir, template_values, check=True)
            metrics = collect_metrics(self.config, run_dir, template_values)
            write_json(run_dir / "metrics.json", metrics)
            review_summary = self.run_review_stage(run_dir, template_values, idea_text, metrics)
            decision = self.evaluate_gate(metrics, review_summary)
            self.finish_run(
                decision=decision,
                run_record=run_record,
                run_dir=run_dir,
                metrics=metrics,
                candidate_changed_paths=candidate_changed_paths,
                parent_branch=parent_branch,
                parent_commit=parent_commit,
            )
            return decision
        except Exception as exc:
            run_record["status"] = "error"
            run_record["error"] = str(exc)
            write_json(run_dir / "run_record.json", run_record)
            append_jsonl(
                self.state_dir / "experiments.jsonl",
                {
                    "run_id": run_id,
                    "status": "error",
                    "parent_commit": parent_commit,
                    "error": str(exc),
                    "finished_at": utc_timestamp(),
                },
            )
            raise

    def create_candidate_branch(self, run_id: str) -> str:
        if not bool(get_config_value(self.config, "git.create_candidate_branch", True)):
            return git_branch(self.project_root)
        prefix = get_config_value(self.config, "git.candidate_branch_prefix", "auto-research")
        branch_name = f"{prefix}/{run_id}"
        git(self.project_root, ["switch", "-c", branch_name], check=True)
        return branch_name

    def run_idea_stage(self, run_dir: Path, template_values: Dict[str, Any]) -> str:
        prompt = self.idea_prompt()
        (run_dir / "idea_prompt.md").write_text(prompt, encoding="utf-8")

        command_template = get_config_value(self.config, "commands.idea")
        if command_template:
            result = run_shell_template(
                "idea",
                command_template,
                template_values,
                cwd=self.project_root,
                log_dir=run_dir / "logs",
                input_text=prompt,
                timeout_seconds=get_config_value(self.config, "timeouts.idea_seconds", 900),
                check=True,
            )
            idea_text = result.stdout.strip()
        else:
            idea_text = (
                "insufficient information: no commands.idea adapter was configured. "
                "The run will rely on the Codex implementation prompt and existing project context."
            )
            (run_dir / "logs" / "idea.stdout.log").parent.mkdir(parents=True, exist_ok=True)
            (run_dir / "logs" / "idea.stdout.log").write_text(idea_text, encoding="utf-8")
            (run_dir / "logs" / "idea.stderr.log").write_text("", encoding="utf-8")

        (run_dir / "idea.md").write_text(idea_text + "\n", encoding="utf-8")
        return idea_text

    def idea_prompt(self) -> str:
        return (
            "你是自动科研系统的创新点生成代理。\n"
            "必须遵守学术诚信：不要编造论文、SOTA、数据或实验结果；证据不足时写 insufficient information。\n\n"
            f"研究目标：{get_config_value(self.config, 'project.objective')}\n"
            f"研究边界：{get_config_value(self.config, 'project.scope', 'not specified')}\n"
            "输出一个可证伪的最小候选创新点，包含：假设、预期机制、需要修改的代码区域、"
            "主指标、关键不退化项、必要消融实验。\n"
        )

    def run_codex_patch_stage(
        self,
        run_dir: Path,
        template_values: Dict[str, Any],
        idea_text: str,
    ) -> None:
        command_template = require_config_value(self.config, "agents.codex_patch_command")
        prompt = self.codex_patch_prompt(run_dir, idea_text)
        prompt_file = run_dir / "codex_patch_prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        values = dict(template_values)
        values["prompt_file"] = prompt_file
        run_shell_template(
            "codex_patch",
            command_template,
            values,
            cwd=self.project_root,
            log_dir=run_dir / "logs",
            input_text=prompt,
            timeout_seconds=get_config_value(self.config, "timeouts.codex_patch_seconds", 1800),
            check=True,
        )

    def codex_patch_prompt(self, run_dir: Path, idea_text: str) -> str:
        allowed_paths = "\n".join(
            f"- {path}" for path in get_config_value(self.config, "files.allowed_paths", [])
        )
        return (
            "你是 Codex 代码实现代理。请只实现一个最小候选实验。\n"
            "硬性规则：\n"
            "1. 不要编造实验结果、SOTA 或引用。\n"
            "2. 只修改 allowlist 中的文件路径。\n"
            "3. 保留无关代码和格式，不做额外重构。\n"
            "4. 若证据不足，明确写 insufficient information，不要臆测。\n"
            "5. 只从当前已验证父节点构造一个候选分支。\n\n"
            f"项目根目录：{self.project_root}\n"
            f"本次 run 目录：{run_dir}\n"
            f"研究目标：{get_config_value(self.config, 'project.objective')}\n"
            f"允许修改路径：\n{allowed_paths}\n\n"
            f"候选创新点/实验计划：\n{idea_text}\n\n"
            "完成后停止，不要运行长训练。"
        )

    def run_command_sequence(
        self,
        name: str,
        run_dir: Path,
        template_values: Dict[str, Any],
        check: bool,
    ) -> List[CommandResult]:
        command_templates = get_config_value(self.config, f"commands.{name}", [])
        if isinstance(command_templates, str):
            command_templates = [command_templates]
        results: List[CommandResult] = []
        for index, command_template in enumerate(command_templates):
            result = run_shell_template(
                f"{name}_{index}",
                command_template,
                template_values,
                cwd=self.project_root,
                log_dir=run_dir / "logs",
                timeout_seconds=get_config_value(self.config, f"timeouts.{name}_seconds"),
                check=check,
            )
            results.append(result)
        return results

    def run_required_command(
        self,
        name: str,
        run_dir: Path,
        template_values: Dict[str, Any],
        check: bool,
    ) -> CommandResult:
        command_template = require_config_value(self.config, f"commands.{name}")
        return run_shell_template(
            name,
            command_template,
            template_values,
            cwd=self.project_root,
            log_dir=run_dir / "logs",
            timeout_seconds=get_config_value(self.config, f"timeouts.{name}_seconds"),
            check=check,
        )

    def run_review_stage(
        self,
        run_dir: Path,
        template_values: Dict[str, Any],
        idea_text: str,
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        diff_text = summarize_diff(self.project_root)
        review_prompt = self.review_prompt(idea_text, metrics, diff_text)
        (run_dir / "review_prompt.md").write_text(review_prompt, encoding="utf-8")

        review_summary: Dict[str, Any] = {}
        codex_reflect_command = get_config_value(self.config, "agents.codex_reflect_command")
        if codex_reflect_command:
            result = run_shell_template(
                "codex_reflection",
                codex_reflect_command,
                template_values,
                cwd=self.project_root,
                log_dir=run_dir / "logs",
                input_text=review_prompt,
                timeout_seconds=get_config_value(self.config, "timeouts.review_seconds", 1200),
                check=False,
            )
            review_summary["codex_reflection_returncode"] = result.returncode

        claude_review_command = get_config_value(self.config, "agents.claude_review_command")
        if claude_review_command:
            result = run_shell_template(
                "claude_review",
                claude_review_command,
                template_values,
                cwd=self.project_root,
                log_dir=run_dir / "logs",
                input_text=review_prompt,
                timeout_seconds=get_config_value(self.config, "timeouts.review_seconds", 1200),
                check=False,
            )
            review_summary["claude_review_returncode"] = result.returncode
            review_summary["claude_review_available"] = result.returncode == 0
        else:
            review_summary["claude_review_available"] = False

        write_json(run_dir / "review_summary.json", review_summary)
        return review_summary

    def review_prompt(self, idea_text: str, metrics: Dict[str, Any], diff_text: str) -> str:
        return (
            "你是独立科研审阅代理。请审查本次候选实验，不要修改文件。\n"
            "审查维度：创新点是否可证伪、实现是否越界、是否存在实验污染、指标是否足以支持晋升、"
            "还缺哪些机制证据和消融。\n"
            "必须遵守学术诚信；证据不足写 insufficient information / uncertain / unknown。\n"
            "请输出简洁 JSON：decision 为 pass/fail/uncertain，issues 为字符串数组。\n\n"
            f"候选创新点：\n{idea_text}\n\n"
            f"已解析指标：\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n\n"
            f"代码 diff 摘要：\n{diff_text}\n"
        )

    def evaluate_gate(self, metrics: Dict[str, Any], review_summary: Dict[str, Any]) -> GateDecision:
        reasons: List[str] = []
        primary_name = require_config_value(self.config, "metrics.primary.name")
        primary_mode = require_config_value(self.config, "metrics.primary.mode")
        min_delta = float(get_config_value(self.config, "metrics.primary.min_delta", 0.0))
        allow_first_parent = bool(get_config_value(self.config, "metrics.primary.allow_first_parent", False))
        candidate_primary = numeric_value(metrics.get(primary_name))
        if candidate_primary is None:
            reasons.append(f"missing primary metric: {primary_name}")
            return GateDecision(False, reasons, metrics)

        configured_baseline = numeric_value(get_config_value(self.config, "metrics.primary.baseline"))
        parent_primary = configured_baseline
        if parent_primary is None:
            parent_primary = load_latest_parent_metric(self.state_dir, primary_name)

        primary_passed, primary_reason = compare_primary_metric(
            candidate_primary,
            parent_primary,
            primary_mode,
            min_delta,
            allow_first_parent,
        )
        reasons.append(primary_reason)

        non_regression_passed = self.evaluate_non_regression(metrics, reasons)
        mechanism_passed = self.evaluate_mechanism_evidence(reasons)
        review_passed = self.evaluate_review_gate(review_summary, reasons)
        promote = primary_passed and non_regression_passed and mechanism_passed and review_passed
        return GateDecision(promote=promote, reasons=reasons, metric_summary=metrics)

    def evaluate_non_regression(self, metrics: Dict[str, Any], reasons: List[str]) -> bool:
        passed = True
        for gate_config in get_config_value(self.config, "metrics.non_regression", []):
            metric_name = gate_config["name"]
            metric_value = numeric_value(metrics.get(metric_name))
            if metric_value is None:
                reasons.append(f"missing non-regression metric: {metric_name}")
                passed = False
                continue
            if "max_value" in gate_config and metric_value > float(gate_config["max_value"]):
                reasons.append(f"{metric_name}={metric_value} exceeds max_value={gate_config['max_value']}")
                passed = False
            if "min_value" in gate_config and metric_value < float(gate_config["min_value"]):
                reasons.append(f"{metric_name}={metric_value} below min_value={gate_config['min_value']}")
                passed = False
        if passed:
            reasons.append("non-regression gates passed")
        return passed

    def evaluate_mechanism_evidence(self, reasons: List[str]) -> bool:
        evidence_files = get_config_value(self.config, "evidence.required_files", [])
        if not evidence_files:
            if bool(get_config_value(self.config, "evidence.require_files", False)):
                reasons.append("mechanism evidence is required but no evidence files are configured")
                return False
            reasons.append("mechanism evidence file gate not configured")
            return True

        passed = True
        for evidence_file in evidence_files:
            evidence_path = self.project_root / evidence_file
            if not evidence_path.exists() or evidence_path.stat().st_size == 0:
                reasons.append(f"missing or empty mechanism evidence file: {evidence_file}")
                passed = False
        if passed:
            reasons.append("mechanism evidence files are present")
        return passed

    def evaluate_review_gate(self, review_summary: Dict[str, Any], reasons: List[str]) -> bool:
        require_claude_review = bool(get_config_value(self.config, "review.require_claude_review", False))
        if require_claude_review and not review_summary.get("claude_review_available"):
            reasons.append("Claude review required but unavailable")
            return False
        reasons.append("review availability gate passed")
        return True

    def finish_run(
        self,
        decision: GateDecision,
        run_record: Dict[str, Any],
        run_dir: Path,
        metrics: Dict[str, Any],
        candidate_changed_paths: Sequence[str],
        parent_branch: str,
        parent_commit: str,
    ) -> None:
        run_record["finished_at"] = utc_timestamp()
        run_record["metrics"] = metrics
        run_record["gate_reasons"] = decision.reasons

        if decision.promote:
            run_record["status"] = "promoted"
            commit_hash = self.promote_candidate(run_record["run_id"], candidate_changed_paths)
            run_record["promotion_commit"] = commit_hash
            append_jsonl(
                self.state_dir / "parents.jsonl",
                {
                    "run_id": run_record["run_id"],
                    "status": "promoted",
                    "commit": commit_hash,
                    "metrics": metrics,
                    "reasons": decision.reasons,
                    "finished_at": run_record["finished_at"],
                },
            )
        else:
            run_record["status"] = "rejected"
            self.rollback_candidate(parent_branch, parent_commit)
            append_text(
                self.state_dir / "DEAD_ENDS.md",
                "\n"
                f"## {run_record['run_id']}\n\n"
                f"- parent_commit: `{parent_commit}`\n"
                f"- metrics: `{json.dumps(metrics, ensure_ascii=False)}`\n"
                f"- reasons: `{json.dumps(decision.reasons, ensure_ascii=False)}`\n"
                "- diagnostic rule: rejected candidates are not valid parents.\n",
            )

        write_json(run_dir / "gate_decision.json", {
            "promote": decision.promote,
            "reasons": decision.reasons,
            "metrics": decision.metric_summary,
        })
        write_json(run_dir / "run_record.json", run_record)
        append_jsonl(self.state_dir / "experiments.jsonl", run_record)

    def promote_candidate(self, run_id: str, candidate_changed_paths: Sequence[str]) -> str:
        if not candidate_changed_paths:
            raise AutoResearchError("No candidate files changed; refusing to promote.")
        git(self.project_root, ["add"] + list(candidate_changed_paths), check=True)
        commit_message = get_config_value(
            self.config,
            "git.promotion_commit_message",
            "auto-research: promote {run_id}",
        ).format(run_id=run_id)
        git(self.project_root, ["commit", "-m", commit_message], check=True)
        return git_head(self.project_root)

    def rollback_candidate(self, parent_branch: str, parent_commit: str) -> None:
        rollback_enabled = bool(get_config_value(self.config, "git.rollback_rejected_candidate", True))
        if not rollback_enabled:
            return
        allowed_paths = get_config_value(self.config, "files.allowed_paths", [])
        if allowed_paths:
            git(self.project_root, ["restore", "--staged", "--worktree", "--"] + allowed_paths, check=False)
        if bool(get_config_value(self.config, "git.return_to_parent_branch", True)):
            current_branch = git_branch(self.project_root)
            if current_branch != parent_branch:
                git(self.project_root, ["switch", parent_branch], check=False)
        if git_head(self.project_root) != parent_commit:
            raise AutoResearchError(
                "Rollback did not return to the parent commit. "
                "Manual review is required before continuing."
            )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a closed-loop automated research cycle.")
    parser.add_argument("--config", required=True, help="Path to YAML or JSON config.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate", help="Validate config and repository access.")
    subparsers.add_parser("init-state", help="Create state files without running experiments.")
    subparsers.add_parser("run-once", help="Run one candidate experiment loop.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    orchestrator = AutoResearchOrchestrator(Path(args.config))

    if args.command == "validate":
        orchestrator.validate_config()
        print("config valid")
        return 0
    if args.command == "init-state":
        orchestrator.init_state()
        print(f"state initialized: {orchestrator.state_dir}")
        return 0
    if args.command == "run-once":
        decision = orchestrator.run_once()
        print(json.dumps({
            "promote": decision.promote,
            "reasons": decision.reasons,
            "metrics": decision.metric_summary,
        }, ensure_ascii=False, indent=2))
        return 0 if decision.promote else 2

    raise AutoResearchError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutoResearchError as error:
        print(f"auto-research error: {error}", file=sys.stderr)
        raise SystemExit(1)
