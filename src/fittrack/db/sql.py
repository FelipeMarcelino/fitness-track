"""Splitting a SQL script into single statements.

asyncpg prepares every statement it sends, and a prepared statement may hold
exactly one command. The schema of spec 5.2 is one script, so it has to arrive
as a list — and it cannot be split on `;`, because its `DO $$ ... $$` blocks
have semicolon-laden bodies that a naive split would cut in half.

This is a lexer, not a parser. It tracks only the four contexts in which a
semicolon is not a boundary: a string literal, a dollar-quoted body, a line
comment and a block comment.
"""

from __future__ import annotations

import re

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_statements(script: str) -> list[str]:
    """The statements of `script`, in order, without their terminating semicolon."""
    statements: list[str] = []
    start = 0
    index = 0
    length = len(script)

    while index < length:
        char = script[index]

        if char == "-" and script.startswith("--", index):
            newline = script.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if char == "/" and script.startswith("/*", index):
            close = script.find("*/", index + 2)
            index = length if close == -1 else close + 2
            continue

        if char == "'":
            index = _skip_quoted(script, index, "'")
            continue

        if char == '"':
            index = _skip_quoted(script, index, '"')
            continue

        if char == "$":
            match = _DOLLAR_TAG.match(script, index)
            if match:
                tag = match.group(0)
                close = script.find(tag, match.end())
                if close == -1:
                    raise ValueError(f"unterminated dollar quote {tag} at offset {index}")
                index = close + len(tag)
                continue

        if char == ";":
            statement = script[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1

        index += 1

    tail = script[start:].strip()
    if tail and not _is_only_comments(tail):
        statements.append(tail)
    return statements


def _skip_quoted(script: str, index: int, quote: str) -> int:
    """Past the closing quote, honouring the doubled-quote escape."""
    index += 1
    while index < len(script):
        if script[index] == quote:
            if index + 1 < len(script) and script[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _is_only_comments(text: str) -> bool:
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    return not stripped.strip()
