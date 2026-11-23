# BridgeCAEBench

A benchmark for large language model (LLM) agents doing bridge-engineering work in structural analysis software.
Agents work in OSIS, bridge analysis software used in production. Each run is scored on the project the agent
leaves in the software, with separate parts for task completion and engineering quality.

This repository accompanies the paper *BridgeCAEBench: benchmarking large language model agents on
bridge-engineering work in structural analysis software*. It contains the cases, checks, scoring configuration and
code, construction tools, the frozen skill library with its bridge templates and reference solutions, and the five agent
designs compared in the paper.

## Cases

359 cases in seven task lines. The 183 model cases are built from the templates of six beam-bridge types: hollow slab,
precast small box girder, precast T-girder, cast-in-place box girder, balanced-cantilever box girder and rigid-frame box
girder.

| Line | Cases | Work |
|---|---:|---|
| Knowledge | 176 | Questions about the software, the design code and modeling practice |
| Build | 55 | Build a model from an engineer's brief |
| Change | 36 | Revise a complete model without breaking the rest |
| Handover | 12 | Finish a model someone else left half-built |
| Diagnose | 44 | Find and repair a failing check, a model that does not run, or a review finding |
| Analyze | 30 | Query results of a solved model and make engineering judgements |
| Project | 6 | One project over several rounds: build, revise, revise, review |

Each case is one YAML file under `datasets/<line>/`, with its start and reference overlays in the directory of the
same name. Only cases with `status: verified` are run.

## Layout

```
datasets/      the cases (knowledge: one question list per source)
skills/        the skill library under test, frozen before the cases were written; bridge templates under
               skills/osis-bridge-*/references/templates are the sources of the reference solutions
scoring/       static extraction, checks, answer scoring, model-conformance scoring per bridge type
tasks/         one module per task line: load cases, prepare the start project, score
methods/       the five agent designs T1-T5 (method.yaml + adapter.py), see docs/methods.md
bench/         scheduling, skill snapshots, MCP and model-gateway proxies, run records, CLI
tools/         case construction and verification tools
configs/       platform.yaml (paths, gateway, MCP), evaluation.yaml (scoring weights), pricing.yaml
docs/          implementation notes on the five designs
```

Code comments, case briefs and the design notes are in Chinese, the working language of the software and its users.

## Requirements

- Windows with OSIS and its OSIS-AI agent, available from https://www.osisbim.com. The OSIS Broker that ships with
  OSIS-AI must be running (default `http://127.0.0.1:18080`); the platform uses it to run and solve the projects.
- [uv](https://docs.astral.sh/uv/) and Python 3.13.
- An OpenAI-compatible gateway for the LLM under test.
- Optional: the OSIS knowledge base, accessible with a registered OSIS account (`OSIS_WEKNORA_API_KEY`).
- T2 needs the upstream OpenCode binary (npm `opencode-ai` 1.19.0); T5 uses the OpenCode build inside the OSIS install.

## Setup

```bash
uv sync --extra test        # main environment: orchestration, T3, tests
uv run osis-bench env       # one venv per framework under .venvs/ (T1/T4: miniswe, T2/T5: opencode)
uv run pytest               # unit tests, no OSIS needed
```

Machine-specific settings and keys go in `configs/platform.local.yaml` (git-ignored), which overrides
`configs/platform.yaml` key by key:

```yaml
secrets:
  OSIS_MODEL_API_KEY: <key>          # injected as an environment variable, never written to run records
runs_root: E:/bench/runs             # OSIS projects of large bridges take several GB per solve
model: {base_url: https://<your-gateway>/v1}
osisai:
  install_dir: <OSIS install>/Rbin64/opencode
  vanilla_opencode: <npm global>/node_modules/opencode-ai/bin/opencode.exe
```

## Running

Every command prints one JSON document.

```bash
uv run osis-bench list                                            # designs and task lines
uv run osis-bench run --task build --label L --dry-run            # size of the matrix
uv run osis-bench run --task build --label L --methods T1 T2 --jobs 3
uv run osis-bench run --task build --label L --model gpt-6.1-sol  # a gateway preset from platform.yaml
uv run osis-bench status --label L
uv run osis-bench report --label L [--markdown]
```

Re-running a label resumes it: finished cells are kept and failed ones are retried. Each cell directory under
`runs/<label>/` keeps the request, the agent's project code, every model and MCP call, and `record.json` with the scores.

To add a design, create `methods/<dir>/method.yaml` and `methods/<dir>/adapter.py` like the existing ones. An adapter
reads the task instructions, the read-only skill snapshot, the project directory, the model gateway and the MCP servers
from the request, and every model and MCP call goes through the platform proxies, so it is recorded the same way as for
the other designs.
