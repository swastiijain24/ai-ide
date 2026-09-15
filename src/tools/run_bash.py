from dataclasses import dataclass
# import os
from pathlib import Path
# import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

# Configurable defaults matching Claude Code specifications
BASH_DEFAULT_TIMEOUT_MS = 120_000                  # 2 minutes
BASH_MAX_TIMEOUT_MS = 600_000                      # 10 minutes ceiling
BASH_MAX_STREAM_BYTES = 5 * 1024 * 1024 * 1024     # 5 GB disk runaway ceiling
BASH_MAX_OUTPUT_LENGTH = 30_000                    # Read-back window (up to 150k)
BASH_OUTPUT_MAX_CHARS = 30_000                     # Inline ceiling before saving to file
BASH_FAILURE_CEILING_CHARS = 10_000                # Error head/tail excerpt budget
BASH_DUMP_FILE_MAX_BYTES = 64 * 1024 * 1024        # 64 MiB log file cap

# Exit code 1 represents benign/informational outcomes for these commands
BENIGN_EXIT_1_COMMANDS = {
    "grep", "rg", "egrep", "fgrep", "find", "diff", "test", "[",
}
BENIGN_GIT_EXIT_1_SUBCMDS = {"diff", "grep"}

# Module-level session state for directory carry-over
_CURRENT_CWD: Path | None = None
_PROJECT_ROOT: Path = Path.cwd().resolve()
_ADDITIONAL_ALLOWED_DIRS: list[Path] = []
_SESSION_LOG_DIR: Path = Path(tempfile.gettempdir()) / "claude_code_session"
_SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)

BASH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "Bash",
    "description": (
        "Executes a bash shell command. Working directory (cd) persists across calls "
        "as long as it remains inside allowed workspace directories. Commands can be run "
        "in the background by setting `run_in_background: true`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command string to execute in bash.",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in milliseconds. Capped by system maximum.",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Set to true to run command as a background task.",
            },
        },
        "required": ["command"],
    },
}

@dataclass 
class ToolResult:
    content: str 
    is_error: bool = False 

def _is_path_allowed(path: Path) ->bool:
    """Verifies target path remains within project root or allowed external directories."""
    resolved = path.resolve()
    allowed_roots = [_PROJECT_ROOT]+ _ADDITIONAL_ALLOWED_DIRS 
    return any(resolved == root or root in resolved.parents for root in allowed_roots)

def _is_benign_exit_1(cmd_str: str) ->bool:
    """Checks if exit code 1 represents a valid informational result (e.g. grep empty)."""
    try:
        tokens = shlex.split(cmd_str.strip())
        if not tokens:
            return False 
        root = Path(tokens[0]).name 
        if root in BENIGN_EXIT_1_COMMANDS:
            return True 
        if root == "git" and len(tokens)>1 and tokens[1] in BENIGN_GIT_EXIT_1_SUBCMDS:
            return True 
    except ValueError:
        pass 
    return False 

def _format_head_tail(text: str, max_chars: int)-> str:
    """Slices failing output into head-and-tail segments with omission notice."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2 
    head = text[:half]
    tail = text[-half:] 
    omitted = len(text) - (len(head)+len(tail))
    return f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"

def execute_bash_tool(
    command: str,
    timeout: int | None = None,
    run_in_background: bool = False,
    default_timeout_ms: int = BASH_DEFAULT_TIMEOUT_MS,
    max_timeout_ms: int = BASH_MAX_TIMEOUT_MS,
    max_stream_bytes: int = BASH_MAX_STREAM_BYTES,
    read_back_chars: int = BASH_MAX_OUTPUT_LENGTH,
    inline_ceiling: int = BASH_OUTPUT_MAX_CHARS,
    failure_ceiling: int = BASH_FAILURE_CEILING_CHARS,
) -> ToolResult:
    """Executes Claude Code's Bash tool with persistent directory tracking and output controls."""
    global _CURRENT_CWD 

    if _CURRENT_CWD is None:
        _CURRENT_CWD = _PROJECT_ROOT 

    raw_timeout = timeout if timeout is not None else default_timeout_ms 
    effective_timeout_ms = min(raw_timeout, max_timeout_ms)
    timeout_seconds = effective_timeout_ms / 1000.0 

    run_id =  uuid.uuid4().hex[:8]
    raw_log_path = _SESSION_LOG_DIR / f"cmd_{run_id}.raw"
    log_handle = open(raw_log_path, "w+b")

    pwd_marker = "__PWD_CAPTURE__"
    wrapped_command = f"{command}\n__RET=$?; echo '{pwd_marker}'\"$PWD\"; exit $__RET"

    try:
        proc = subprocess.Popen(
            ["bash", "-c", wrapped_command],
            cwd=_CURRENT_CWD,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )
    except Exception as exc:
        log_handle.close()
        return ToolResult(content=f"Error starting bash command: {exc}", is_error=True)

    if run_in_background:
        return ToolResult(
            content=(
                f"Command started in background.\n"
                f"Task ID: task_{run_id}\n"
                f"Output streaming to: {raw_log_path}"
            )
        )

    start_time = time.time()
    hit_timeout = False
    hit_disk_ceiling = False

    while proc.poll() is None:
        time.sleep(0.05)

        try:
            if  raw_log_path().st_size > max_stream_bytes:
                hit_disk_ceiling = True
                proc.kill()
                break 
        except FileNotFoundError:
            pass 

        if (time.time()-start_time) > timeout_seconds:
            hit_timeout = True 
            break 

    if hit_timeout:
        if command.strip().startswith("sleep"):
            proc.kill()
            log_handle.close()
            return ToolResult(
                content=f"Error: Command timed out after {int(timeout_seconds)}s and was killed.",
                is_error=True,
            ) 

        log_handle.close()
        return ToolResult(
            content=(
                f"Command did not complete within its {int(timeout_seconds)}s timeout "
                "and was moved to the background.\n"
                f"Task ID: task_{run_id}\n"
                f"Output being written to: {raw_log_path}\n"
                f"Session cwd remains {_CURRENT_CWD}; directory changes made by the "
                "backgrounded command do not apply to subsequent commands."
            )
        )

    log_handle.flush()
    log_handle.close()

    if hit_disk_ceiling:
        return ToolResult(
            content=f"Error: Command exceeded the {max_stream_bytes // (1024**3)} GB output limit and was killed.",
            is_error=True,
        )

    try:
        with open(raw_log_path, "r", encoding="utf-8", errors="replace") as f:
            output_str = f.read(read_back_chars)
    except OSError as exc:
        output_str = f"<Unable to read command log: {exc}>"

    exit_code = proc.returncode 

    cwd_notice = ""
    if pwd_marker in output_str:
        parts = output_str.split(pwd_marker)
        output_str = parts[0]
        new_pwd_str = parts[1].splitlines()[0].strip() if len(parts) > 1 else ""
    
        if new_pwd_str:
            new_path = Path(new_pwd_str).resolve()
            if  _is_path_allowed(new_path):
                _CURRENT_CWD = new_path
            else:
                _CURRENT_CWD = _PROJECT_ROOT
                cwd_notice = f"\nShell cwd was reset to {_PROJECT_ROOT}"

    is_valid = (exit_code == 0) or (exit_code == 1 and _is_benign_exit_1(command))

    if is_valid:
        if len(output_str) <=inline_ceiling:
            final_content = output_str or  "<Command produced no output>" 
        else:
            dump_file = _SESSION_LOG_DIR / f"bash_output_{run_id}.log" 
            try:
                with open(raw_log_path, "rb") as src, open(dump_file, "wb") as dst:
                    dst.write(src.read(BASH_DUMP_FILE_MAX_BYTES))
            except OSError:
                pass 

            preview = output_str[:1500]
            final_content = (
                f"{preview}\n\n... [Output truncated. Full output saved to: {dump_file}. "
                "Use Read or Grep to inspect the rest.]"
            )
    else:
        final_content = f"Command failed with exit code {exit_code}:\n" + _format_head_tail(
            output_str, failure_ceiling
        )

    if cwd_notice:
        final_content += cwd_notice 

    return ToolResult(content=final_content, is_error=not is_valid)