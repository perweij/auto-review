"""Provider commands and environment policies, independent of process lifecycle."""
from dataclasses import dataclass
import json
import os
import re

from .common import ApiError, ROOT


DENIED_TOOLS = ["Edit", "Write", "NotebookEdit", "Bash(git push:*)", "Bash(git commit:*)",
                "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git reset:*)",
                "Bash(git restore:*)", "Bash(git stash:*)", "Bash(gh:*)"]


@dataclass
class Runtime:
    permission: str = "auto"
    model_timeout: object = None
    auth_timeout: int = 60
    budget: str = ""
    probe_model: str = "gpt-6-luna"

    @classmethod
    def environment(cls):
        for name in ("PR_REVIEW_MODEL_TIMEOUT", "PR_REVIEW_AUTH_TIMEOUT"):
            value = os.environ.get(name)
            if value and not re.fullmatch(r"[1-9][0-9]*", value):
                raise ApiError("invalid_request", f"{name} must be positive integer seconds")
        budget = os.environ.get("PR_REVIEW_CLAUDE_BUDGET", "")
        if budget and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", budget):
            raise ApiError("invalid_request", "PR_REVIEW_CLAUDE_BUDGET must be a USD amount")
        permission = os.environ.get("PR_REVIEW_CLAUDE_PERMISSION_MODE") or "auto"
        if permission not in ("auto", "dontAsk", "manual", "plan"):
            raise ApiError("invalid_request", "Unsupported Claude permission mode")
        return cls(permission, int(os.environ["PR_REVIEW_MODEL_TIMEOUT"]) if os.environ.get("PR_REVIEW_MODEL_TIMEOUT") else None,
                   int(os.environ.get("PR_REVIEW_AUTH_TIMEOUT") or "60"), budget,
                   os.environ.get("PR_REVIEW_CODEX_PROBE_MODEL") or "gpt-6-luna")


def environment(provider):
    result, removed = dict(os.environ), []
    allowed = {"claude": {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"},
               "codex": {"OPENAI_API_KEY", "CODEX_API_KEY"}}[provider]
    for name in list(result):
        upper = name.upper()
        git_override = upper in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS") or upper.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        secret = re.search(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY(?:_|$)", upper)
        if git_override or (secret and name not in allowed):
            removed.append(name)
            result.pop(name)
    result.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
    return result, sorted(removed)


def provider_for(settings, stage):
    return stage if stage in ("claude", "codex") else settings["processing_provider"]


def review_command(settings, runtime, stage, directory, base_ref):
    text = lambda name: (ROOT / "prompts" / name).read_text().rstrip("\n")
    codex = ["codex", "exec", "-m", settings["codex_model"], "--strict-config",
             "-c", "model_reasoning_effort=" + settings["codex_effort"],
             "-c", "sandbox_mode=read-only", "-c", "approval_policy=never"]
    if stage == "codex":
        codex.insert(2, "review")
        return codex + ["-c", "developer_instructions=" + json.dumps((ROOT / "prompts/independent.txt").read_text(), ensure_ascii=False),
                        "--ephemeral", "--json", "-o", str(directory / "reviews/codex.md.tmp"), "--base", base_ref]
    if stage != "claude" and settings["processing_provider"] == "codex":
        return codex + ["--ephemeral", "--json", "--output-schema", str(ROOT / f"prompts/{stage}.schema.json"),
                        "-o", str(directory / f"reviews/{stage}.codex.tmp"), "-"]
    args = ["claude", "-p", "--model", settings["claude_model"], "--effort", settings["claude_effort"],
            "--permission-mode", runtime.permission, "--no-session-persistence"]
    if runtime.budget:
        args += ["--max-budget-usd", runtime.budget]
    if stage == "claude":
        return args + ["--output-format", "stream-json", "--verbose", "--append-system-prompt", text("independent.txt"),
                       "--disallowed-tools", *DENIED_TOOLS, "--", f"/code-review {settings['claude_effort']} --max-findings default {base_ref}"]
    if stage in ("synthesis", "followup"):
        policy = "focused" if stage == "followup" else settings["synthesis_tests"]
        args += ["--append-system-prompt", text(f"synthesis-testing-{policy}.txt")]
    args += ["--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--json-schema", text(stage + ".schema.json"), "--disallowed-tools", *DENIED_TOOLS]
    directories = {"suggestions": [], "suggestion-verify": [],
                   "synthesis": ["context", "reviews"], "contributions": ["discussion", "reviews"],
                   "followup": ["discussion", "reviews", "context"]}[stage]
    if stage == "synthesis" and (directory / "reviews/reassessment-input.json").is_file():
        directories += ["discussion"]
    for name in directories:
        args += ["--add-dir", str(directory / name)]
    return args


def cache_command(argv):
    structured = argv[0] == "claude" and "--json-schema" in argv
    result, skip, output_format = [], False, False
    for arg in argv:
        if skip:
            skip = False
        elif output_format:
            result.append("json")
            output_format = False
        elif arg == "--max-budget-usd":
            skip = True
        elif structured and arg in ("--verbose", "--include-partial-messages"):
            continue
        else:
            result.append(arg)
            output_format = structured and arg == "--output-format"
    return result


def probe_command(provider, runtime, response):
    prompt = "Reply with exactly PR_REVIEW_AUTH_OK. Do not use tools or inspect files."
    if provider == "claude":
        return ["claude", "-p", "--model", "haiku", "--effort", "low", "--permission-mode", "dontAsk",
                "--no-session-persistence", "--output-format", "json", "--tools", "", "--disable-slash-commands",
                "--strict-mcp-config", "--setting-sources", "user", "--system-prompt",
                "You are an authentication diagnostic. Follow the requested output exactly.", "--", prompt]
    return ["codex", "exec", "-m", runtime.probe_model, "--strict-config", "--skip-git-repo-check",
            "-c", "model_reasoning_effort=low", "-c", "sandbox_mode=read-only", "-c", "approval_policy=never",
            "-c", "project_doc_max_bytes=0", "--ephemeral", "--json", "-o", str(response), "--", prompt]


def adaptation_command(settings):
    provider = settings["processing_provider"]
    model, effort = settings[provider + "_model"], settings[provider + "_effort"]
    if provider == "codex":
        return ["codex", "exec", "-m", model, "--strict-config", "--skip-git-repo-check", "-c", "model_reasoning_effort=" + effort,
                "-c", "sandbox_mode=read-only", "-c", "approval_policy=never", "-c", "project_doc_max_bytes=0", "--ephemeral", "--json", "-"]
    return ["claude", "-p", "--model", model, "--effort", effort, "--tools", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence", "--output-format", "json"]
