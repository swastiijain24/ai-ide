from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

# Token ceiling equivalent in characters (approx. 4 chars/token; 25,000 tokens ≈ 100 KB)
MAX_TOOL_BYTES = 100_000

READ_TOOL_SCHEMA: dict[str, Any] = {
    "name": "Read",
    "description": (
        "Reads file contents with 1-based line numbers prefixed. "
        "Paths must be absolute. Defaults to reading from line 1. "
        "Returns a PARTIAL view notice if the whole file exceeds output limits."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "The 1-based line number to start reading from.",
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
                "minimum": 1,
            },
        },
        "required": ["file_path"],
    },
}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


def execute_read_tool(
    file_path: str,
    offset: int | None = None,
    limit: int | None = None,
    byte_ceiling: int = MAX_TOOL_BYTES,
) -> ToolResult:
    """Executes Claude Code's Read tool according to v2.1.208 specifications."""
    target_path = Path(file_path)

    # 1. Claude must pass absolute paths
    if not target_path.is_absolute():
        return ToolResult(
            content=f"Error: `file_path` must be an absolute path. Received: '{file_path}'",
            is_error=True,
        )

    # 2. Read only reads files, not directories
    if target_path.is_dir():
        return ToolResult(
            content=(
                f"Error: '{file_path}' is a directory. "
                "Read only operates on files; list directory contents with Bash (e.g. `ls`)."
            ),
            is_error=True,
        )

    if not target_path.exists():
        return ToolResult(
            content=f"Error: File not found: '{file_path}'",
            is_error=True,
        )

    # 3. Handle 0-byte / Empty File Notice
    if target_path.stat().st_size == 0:
        return ToolResult(
            content=f"Notice: The file '{file_path}' exists, but its contents are empty."
        )

    is_unbounded_whole_file = offset is None and limit is None
    start_line = offset if offset is not None else 1
    max_lines = limit if limit is not None else float("inf")

    formatted_lines: list[str] = []
    total_lines = 0
    accumulated_bytes = 0
    hit_limit_early = False

    try:
        with open(target_path, mode="r", encoding="utf-8", errors="replace") as f:
            for current_line_no, raw_line in enumerate(f, start=1):
                total_lines = current_line_no
                line = raw_line.rstrip("\r\n")

                if current_line_no < start_line:
                    continue

                if len(formatted_lines) < max_lines:
                    # Early guard: Check if a single line or current range exceeds the token/byte budget
                    line_entry = f"{current_line_no:>6}\t{line}\n"
                    entry_size = len(line_entry.encode("utf-8"))

                    # If an explicit offset/limit was set and this single range exceeds budget
                    if not is_unbounded_whole_file and (accumulated_bytes + entry_size > byte_ceiling):
                        if len(formatted_lines) == 0:
                            # The very first line in the range was so huge it broke the budget
                            return ToolResult(
                                content=(
                                    f"Error: Line {current_line_no} exceeds the maximum token limit. "
                                    "A single line is too large to display. "
                                    "Search for specific content using `Grep` instead."
                                ),
                                is_error=True,
                            )
                        # Explicit limit was too large
                        return ToolResult(
                            content=(
                                f"Error: Reading {limit} lines from offset {offset} exceeded the token limit. "
                                "Use a smaller `limit` or narrow the range."
                            ),
                            is_error=True,
                        )

                    # For unbounded whole-file reads: stop and prepare the PARTIAL view notice
                    if is_unbounded_whole_file and (accumulated_bytes + entry_size > byte_ceiling):
                        hit_limit_early = True
                        break

                    formatted_lines.append(f"{current_line_no:>6}\t{line}")
                    accumulated_bytes += entry_size

    except OSError as exc:
        return ToolResult(
            content=f"Error reading file '{file_path}': {exc}",
            is_error=True,
        )

    # 4. Offset past the end notice (distinguished from empty file)
    if start_line > total_lines:
        return ToolResult(
            content=(
                f"Notice: Offset {start_line} is beyond the end of the file. "
                f"The file '{file_path}' contains {total_lines} lines in total."
            )
        )

    content = "\n".join(formatted_lines)

    # 5. Whole-file PARTIAL view notice
    if is_unbounded_whole_file and hit_limit_early:
        last_rendered_line = len(formatted_lines)
        content += (
            f"\n\n[PARTIAL view: Received lines 1-{last_rendered_line}. "
            "File exceeds tool output limits. "
            f"Read subsequent chunks using `offset={last_rendered_line + 1}` and `limit`.]"
        )

    return ToolResult(content=content)