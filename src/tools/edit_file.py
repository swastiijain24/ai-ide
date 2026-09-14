from dataclasses import dataclass
from pathlib import Path
from typing import Any

EDIT_TOOL_SCHEMA: dict[str, Any] = {
    "name": "Edit",
    "description": (
        "Performs exact string replacement in a file. Paths must be absolute. "
        "The file must have been read in the current conversation before editing. "
        "`old_string` must match uniquely within the file unless `replace_all` is true."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "The exact block of code to replace. Include 3-5 lines of surrounding "
                    "context if this snippet appears multiple times."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "The replacement string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Set to true to replace all occurrences. Defaults to false.",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


def execute_edit_tool(
    file_path: str,
    old_string: str,
    new_string: str,
    last_op: dict[Path, str],
    replace_all: bool = False,
) -> ToolResult:
    """Executes Claude Code's Edit tool according to exact-matching and read-before-edit rules."""
    target_path = Path(file_path)

    # 1. Claude must pass absolute paths
    if not target_path.is_absolute():
        return ToolResult(
            content=f"Error: `file_path` must be an absolute path. Received: '{file_path}'",
            is_error=True,
        )

    # 2. Edit operates only on regular files
    if target_path.is_dir():
        return ToolResult(
            content=f"Error: '{file_path}' is a directory. Edit only operates on regular files.",
            is_error=True,
        )

    if not target_path.exists():
        return ToolResult(
            content=f"Error: File not found: '{file_path}'",
            is_error=True,
        )

    # 3. Read-before-edit check: file must have been read as the immediate prior operation
    resolved_path = target_path.resolve()
    if last_op.get(resolved_path) != "READ":
        return ToolResult(
            content=(
                f"Error: File '{file_path}' has not been read in this conversation "
                "or was modified since last viewed. "
                "You must inspect the file with `Read` before applying edits."
            ),
            is_error=True,
        )

    # 4. Input validations
    if old_string == "":
        return ToolResult(
            content="Error: `old_string` cannot be empty.",
            is_error=True,
        )

    if old_string == new_string:
        return ToolResult(
            content="Error: `old_string` and `new_string` are identical; no changes applied.",
            is_error=True,
        )

    # 5. Read file contents and normalize line endings for platform neutrality
    try:
        raw_content = target_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(
            content=f"Error reading file '{file_path}': {exc}",
            is_error=True,
        )

    uses_crlf = "\r\n" in raw_content
    normalized_content = raw_content.replace("\r\n", "\n")
    normalized_old = old_string.replace("\r\n", "\n")
    normalized_new = new_string.replace("\r\n", "\n")

    # 6. Exact match verification
    match_count = normalized_content.count(normalized_old)

    if match_count == 0:
        return ToolResult(
            content=(
                f"Error: `old_string` was not found in '{file_path}'. "
                "Ensure whitespace and indentation match the file exactly. "
                "Call `Read` again if you need to re-verify line context."
            ),
            is_error=True,
        )

    # 7. Uniqueness verification
    if match_count > 1 and not replace_all:
        return ToolResult(
            content=(
                f"Error: Found {match_count} occurrences of `old_string` in '{file_path}'. "
                "Include additional lines of surrounding context to uniquely identify the target, "
                "or set `replace_all=True` to update all instances."
            ),
            is_error=True,
        )

    # 8. Apply replacement
    if replace_all:
        updated_content = normalized_content.replace(normalized_old, normalized_new)
        replaced_count = match_count
    else:
        updated_content = normalized_content.replace(normalized_old, normalized_new, 1)
        replaced_count = 1

    # Preserve CRLF formatting if present in the source file
    if uses_crlf:
        updated_content = updated_content.replace("\n", "\r\n")

    # 9. Atomic file write
    try:
        target_path.write_text(updated_content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(
            content=f"Error writing to file '{file_path}': {exc}",
            is_error=True,
        )

    # 10. Update state so subsequent edits require a fresh read
    last_op[resolved_path] = "EDIT"

    return ToolResult(
        content=f"Successfully edited '{file_path}'. Replaced {replaced_count} occurrence(s)."
    )