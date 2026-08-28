"""The domain must not know which channel a message came from (AD-39, spec 18.1).

The difference between Telegram and WhatsApp is *format*, decided at the very
end, and never *content*, decided in the middle. Two modules are allowed to know
the difference — `voice`, which chooses what the user sees, and `deliver`, which
speaks the protocol — and everything else must not.

This is the cheapest of the architecture guardrails and it covers the most
expensive regression: an `import` of `channels` inside a subgraph takes seconds
to detect and weeks to undo once three features are built on top of it (§21.4).

The checker is tested against sources that *do* violate the rule, so it cannot
quietly pass because the packages it guards are still empty.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Where the rule applies.
GUARDED = ("fittrack/graph", "fittrack/agents")

# The two exceptions of spec 13.5 and 18.1, by path. Named rather than
# pattern-matched: an exception that can be earned by naming a file
# `something_voice.py` is not an exception, it is a loophole.
EXCEPTIONS = frozenset({"fittrack/graph/nodes/voice.py", "fittrack/graph/nodes/deliver.py"})

FORBIDDEN_MODULE = "fittrack.channels"
FORBIDDEN_ATTRIBUTE = "channel_caps"


def imports_channels(source: str) -> bool:
    """Whether a module imports from `fittrack.channels`, in any spelling."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith(FORBIDDEN_MODULE) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            # `from fittrack.channels import x`, and the relative spellings that
            # resolve to it — `from ..channels import x` inside `graph/`.
            module = node.module or ""
            if module.startswith(FORBIDDEN_MODULE) or module.endswith("channels"):
                return True
            if node.level and module in {"", "channels"}:
                return True
    return False


def reads_channel_caps(source: str) -> bool:
    """Whether a module reads `channel_caps`, by attribute or by key."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == FORBIDDEN_ATTRIBUTE:
            return True
        if isinstance(node, ast.Name) and node.id == FORBIDDEN_ATTRIBUTE:
            return True
        if isinstance(node, ast.arg) and node.arg == FORBIDDEN_ATTRIBUTE:
            # A subgraph that accepts the capabilities as a parameter is reading
            # them, whoever passed them in.
            return True
        if isinstance(node, ast.Constant) and node.value == FORBIDDEN_ATTRIBUTE:
            # `state["channel_caps"]` — the spelling a subgraph would actually
            # use, since the state is a TypedDict rather than an object.
            return True
    return False


def guarded_modules() -> list[Path]:
    return sorted(
        path
        for package in GUARDED
        for path in (SRC / package).rglob("*.py")
        if path.relative_to(SRC).as_posix() not in EXCEPTIONS
    )


# --------------------------------------------------------------------------- #
# The checker itself, against sources that do violate the rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "source",
    [
        "import fittrack.channels",
        "import fittrack.channels.telegram.adapter",
        "from fittrack.channels import ChannelCaps",
        "from fittrack.channels.telegram import adapter",
        "from ..channels import base",
        "from ...fittrack.channels import base",
    ],
    ids=[
        "plain import",
        "deep import",
        "from import",
        "from submodule",
        "relative",
        "deep relative",
    ],
)
def test_the_checker_catches_an_import(source: str) -> None:
    assert imports_channels(source)


@pytest.mark.parametrize(
    "source",
    [
        "import fittrack.agents",
        "from fittrack.domain import models",
        "from fittrack.llm.roles import LLMRole",
        "import os",
    ],
)
def test_the_checker_allows_what_it_should(source: str) -> None:
    assert not imports_channels(source)


@pytest.mark.parametrize(
    "source",
    [
        "caps = state['channel_caps']",
        "caps = state.channel_caps",
        "def f(channel_caps): pass",
        'x = {"channel_caps": 1}',
    ],
)
def test_the_checker_catches_a_capabilities_read(source: str) -> None:
    assert reads_channel_caps(source)


def test_the_checker_allows_unrelated_attributes() -> None:
    assert not reads_channel_caps("caps = state['profile']\nx = state.origin_channel")


# --------------------------------------------------------------------------- #
# The rule, against the real tree
# --------------------------------------------------------------------------- #


def test_no_guarded_module_imports_a_channel() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in guarded_modules()
        if imports_channels(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{offenders} import from channels/. The domain decides content; only "
        "voice and deliver decide format (AD-39)."
    )


def test_no_guarded_module_reads_the_capabilities() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in guarded_modules()
        if reads_channel_caps(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{offenders} read channel_caps. It is read in exactly two places: "
        "voice_agent and the output adapter (spec 18.1)."
    )


def test_the_guarded_packages_exist() -> None:
    """Otherwise the two tests above pass over nothing at all."""
    for package in GUARDED:
        assert (SRC / package).is_dir(), f"{package} is missing"


def test_the_exceptions_are_named_and_few() -> None:
    """Two, per spec 13.5 and 18.1. A third needs a decision, not a commit."""
    assert {
        "fittrack/graph/nodes/voice.py",
        "fittrack/graph/nodes/deliver.py",
    } == EXCEPTIONS
