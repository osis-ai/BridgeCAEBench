"""T1: mini-SWE-agent (pip ``mini-swe-agent``, its ``mini.yaml`` config): one bash tool and nothing else.

Skills and the candidate project are plain files; MCP servers are reached with the ``osis-mcp`` command line
(methods/mcp_cli.py) through the run's proxies. No step or cost limit: the task line's wall time stops it.
It has no compaction of its own: near ``model.context_tokens`` the oldest turns are left out of the call (``Model``).
T4 (verification gate) wraps this agent unchanged: same prompt, same session (``resume``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from methods._common import (base_metadata, end_turn, fail, finish, main, model_settings, normalize_tokens,
                             package_version)

FINAL = "final_answer.md"
SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
Runtime = Callable[[dict[str, Any], str], dict[str, Any]]
OUTPUT_RESERVE = 32000  # room kept for the reply, as OpenCode keeps before it compacts
DROPPED = "\n\n（上下文已达上限，前面较早的 {n} 条对话已省略，只保留最近的。）"


def _model_class() -> type:
    from minisweagent.models.litellm_model import LitellmModel

    class Model(LitellmModel):
        """Sends the system prompt, the task and the latest turns that fit ``cap``; the trajectory keeps them all.

        Once a call would pass cap - OUTPUT_RESERVE, the cut moves forward to a turn start leaving about half the cap,
        so the sent prefix stays the same (and cached) for a while. Tokens per character come from the last call.
        """

        cap = 0
        cut = 2  # index of the first turn sent after the task
        cuts = 0
        per_char = 0.5  # until the first reply calibrates it
        sent_chars = 1

        def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
            prepared = super()._prepare_messages_for_api(messages)
            size = [len(json.dumps(m, ensure_ascii=False)) for m in prepared]
            if self.cap and self.per_char * (sum(size[:2]) + sum(size[self.cut:])) > self.cap - OUTPUT_RESERVE:
                budget = self.cap / 2 / self.per_char - sum(size[:2])
                starts = [i for i in range(self.cut + 1, len(prepared)) if prepared[i].get("role") == "assistant"]
                if starts:
                    self.cut = next((i for i in starts if sum(size[i:]) <= budget), starts[-1])
                    self.cuts += 1
            kept = prepared[:2] + prepared[self.cut:]
            if self.cut > 2:
                kept[1] = {**kept[1], "content": str(kept[1].get("content") or "") + DROPPED.format(n=self.cut - 2)}
            self.sent_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in kept)
            return kept

        def query(self, messages: list[dict], **kwargs: Any) -> dict:
            message = super().query(messages, **kwargs)
            usage = ((message.get("extra") or {}).get("response") or {}).get("usage") or {}
            if usage.get("prompt_tokens"):
                self.per_char = usage["prompt_tokens"] / self.sent_chars
            return message

    return Model


def workspace(request: dict[str, Any]) -> Path:
    path = Path(request.get("work_dir") or Path(request["run_dir"]) / "t1_workspace")
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_text(request: dict[str, Any]) -> str:
    """The task's instructions plus how this harness reaches skills, MCP and the final reply (shared by T1 and T4)."""
    final = Path(request["run_dir"]) / FINAL
    names = ", ".join(sorted(request.get("mcp_servers") or {})) or "（本任务没有）"
    return request["instructions"] + f"""

---
本方法只有 bash 一个工具（Git Bash，每条命令在新的子 shell 里执行，工作目录 {workspace(request)}）。
- skill 快照是普通文件，在 {request['skills_dir']}，只读：用 ls / cat / grep 读，不要修改。上文说的“提供的工具”就是在这个目录上用 bash。
- MCP 服务用命令行 osis-mcp 调用：
    osis-mcp list-tools [server]                  # 列出工具和参数
    osis-mcp call <server> <tool> '<json-args>'   # 调用一个工具，打印返回的文本
  可用服务：{names}。
- 完成后把最终回复写进 {final.as_posix()}：第一行写 `status: done`（完成）或 `status: declined`（简报不合理、拒做，
  并写明理由），后面按任务要求的格式写。然后单独执行：echo {SUBMIT} && cat {final.as_posix()}
"""


def _bash() -> str:
    """Git Bash (not WSL's bash.exe in System32)."""
    git = shutil.which("git")
    for parent in Path(git).resolve().parents if git else []:
        if (parent / "bin" / "bash.exe").is_file():
            return str(parent / "bin" / "bash.exe")
    return shutil.which("bash") or "bash"


def _use_bash() -> None:
    """mini-SWE-agent runs actions with ``shell=True`` (cmd.exe on Windows); run them in bash instead."""
    from minisweagent.environments import local

    bash = _bash()

    def run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen([bash, "-c", command], text=True, cwd=cwd, env=env, encoding="utf-8",
                                   errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            stdout, _ = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout, output=stdout)
        return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)

    local._run = run


def _osis_mcp(request: dict[str, Any]) -> dict[str, str]:
    """Put ``osis-mcp`` on PATH for the agent's shell; returns the env the commands run with."""
    bin_dir = Path(request["run_dir"]) / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "osis-mcp").write_text(
        f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" -m methods.mcp_cli "$@"\n', encoding="utf-8")
    root = str(Path(__file__).resolve().parents[2])
    return {"PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
            "PYTHONPATH": os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]), "PYTHONIOENCODING": "utf-8",
            "OSIS_MCP_SERVERS": json.dumps({n: s["url"] for n, s in (request.get("mcp_servers") or {}).items()}),
            "OSIS_MCP_TIMEOUT_S": str(int(request["timeout_s"]))}


def make_agent(request: dict[str, Any]) -> Any:
    import yaml
    from minisweagent.agents.default import AgentConfig, DefaultAgent
    from minisweagent.environments.local import LocalEnvironment
    class Agent(DefaultAgent):
        def add_messages(self, *messages: dict) -> list[dict]:
            for m in messages:  # thinking mode rejects a history whose tool-call turn has no reasoning_content
                if m.get("role") == "assistant" and m.get("tool_calls") and m.get("reasoning_content") is None:
                    m["reasoning_content"] = ""
            return super().add_messages(*messages)

        def resume(self, feedback: str) -> dict:
            """Continue the same conversation after a submission with one more user message."""
            if self.messages and self.messages[-1].get("role") == "exit":
                self.messages.pop()
            last = next((m for m in reversed(self.messages) if m.get("role") == "assistant"), None)
            if last is not None:  # the submit command's tool call still needs its result
                self.add_messages(*self.model.format_observation_messages(
                    last, [{"output": SUBMIT, "returncode": 0, "exception_info": ""}], self.get_template_vars()))
            self.add_messages(self.model.format_message(role="user", content=feedback))
            return self._loop()

        def run(self, task: str = "", **kwargs: Any) -> dict:
            self.extra_template_vars |= {"task": task, **kwargs}
            self.messages = []
            self.add_messages(
                self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
                self.model.format_message(role="user", content=self._render_template(self.config.instance_template)))
            return self._loop()

        def _loop(self) -> dict:  # DefaultAgent.run's loop, without resetting the conversation
            from minisweagent.exceptions import FormatError, InterruptAgentFlow

            while True:
                try:
                    self.step()
                    self.n_consecutive_format_errors = 0
                except FormatError as e:
                    self.n_consecutive_format_errors += 1
                    self.add_messages(*e.messages)
                    if 0 < self.config.max_consecutive_format_errors <= self.n_consecutive_format_errors:
                        self.add_messages({"role": "exit", "content": "RepeatedFormatError",
                                           "extra": {"exit_status": "RepeatedFormatError", "submission": ""}})
                except InterruptAgentFlow as e:
                    self.add_messages(*e.messages)
                finally:
                    self.save(self.config.output_path)
                if self.messages[-1].get("role") == "exit":
                    return self.messages[-1].get("extra", {})

    _use_bash()
    config = yaml.safe_load((resources.files("minisweagent") / "config" / "mini.yaml").read_text(encoding="utf-8"))
    settings = model_settings(request)
    os.environ["OPENAI_API_KEY"] = settings["api_key"]  # not in model_kwargs: those are saved to trajectory.json
    kwargs: dict[str, Any] = {**(config["model"].get("model_kwargs") or {}), "api_base": settings["base_url"],
                              "seed": settings["seed"], "timeout": settings["timeout"],
                              "reasoning_effort": settings["reasoning_effort"]}
    if settings["temperature"] is not None:
        kwargs["temperature"] = settings["temperature"]
    if settings["max_tokens"] is not None:
        kwargs["max_tokens"] = settings["max_tokens"]
    model = _model_class()(**{**config["model"], "model_kwargs": kwargs}, model_name=f"openai/{settings['model']}",
                           cost_tracking="ignore_errors")
    model.cap = int(request["model"].get("context_tokens") or 0)
    env = LocalEnvironment(cwd=str(workspace(request)), timeout=int(request["timeout_s"]),
                           env={**(config["environment"].get("env") or {}), **_osis_mcp(request)})
    agent_cfg = {k: v for k, v in config["agent"].items() if k in AgentConfig.model_fields}
    # mini.yaml's instance template is a SWE-bench issue workflow; the task text carries ours
    agent_cfg.update(instance_template="{{task}}", step_limit=0, cost_limit=0.0,
                     wall_time_limit_seconds=max(60, int(request["timeout_s"]) - 60),
                     output_path=Path(request["run_dir"]) / "trajectory.json")
    return Agent(model, env, **agent_cfg)


def summarize(agent: Any, exit_info: dict[str, Any]) -> dict[str, Any]:
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    tool_calls = 0
    for message in agent.messages:
        extra = message.get("extra") or {}
        usage = normalize_tokens((extra.get("response") or {}).get("usage") or {})
        for key in tokens:
            tokens[key] += usage.get(key, 0)
        tool_calls += len(extra.get("actions") or [])
    # the submit marker only counts as the output's first line; the file the task asked for is the fallback
    final = Path(agent.config.output_path).parent / FINAL  # trajectory.json sits in the run dir
    submission = str(exit_info.get("submission") or "").strip()
    if not submission and final.is_file():
        submission = final.read_text(encoding="utf-8", errors="replace").strip()
    return {"final_answer": submission, "exit_status": exit_info.get("exit_status"),
            "model_calls": agent.n_calls, "tool_calls": tool_calls, "tokens": tokens,
            "context_cuts": agent.model.cuts}


def next_turn(text: str) -> str:
    """A later project turn, sent into the same conversation (T4 too)."""
    return text + "\n\n完成后照前面的要求重写最终回复文件并提交。"


def _runtime(request: dict[str, Any], prompt: str) -> dict[str, Any]:
    agent = make_agent(request)
    exit_info = agent.run(prompt)
    for text in [*(request.get("turns") or []), None]:  # project: one more user message per turn
        if exit_info.get("exit_status") != "Submitted":  # out of time or format errors: the session is over
            break
        end_turn(request, str(exit_info.get("submission") or ""))
        if text is None:
            break
        exit_info = agent.resume(next_turn(text))
    return summarize(agent, exit_info)


def run(request: dict[str, Any], *, runtime: Runtime | None = None) -> dict[str, Any]:
    started = time.monotonic()
    metadata = base_metadata(request, framework="mini-swe-agent", framework_version=package_version("mini-swe-agent"),
                             interaction_mode="bash_loop", tools=["bash"])
    try:
        metadata.update((runtime or _runtime)(request, task_text(request)))
        if not str(metadata.get("final_answer") or "").strip():
            raise RuntimeError(f"mini-SWE-agent ended without a submission ({metadata.get('exit_status')})")
        metadata.update(status="completed", stop_reason="completed")
    except Exception as exc:  # noqa: BLE001
        fail(metadata, exc)
    return finish(request, metadata, started)


if __name__ == "__main__":
    raise SystemExit(main(run))
