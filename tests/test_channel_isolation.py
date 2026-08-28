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
FORBIDDEN_SEGMENTS = FORBIDDEN_MODULE.split(".")
FORBIDDEN_ATTRIBUTE = "channel_caps"


def names_channels(module: str, *, relative: bool) -> bool:
    """Whether a dotted module path refers to the channels package.

    Segment-wise rather than by prefix or suffix. `endswith("channels")` was
    both too loose — a hypothetical `subchannels` would match — and too tight:
    it missed `from ..channels.telegram import X`, where the path ends in the
    submodule, not in `channels`. That spelling is the natural one to reach for
    from inside `graph/`, and it was the one getting through.
    """
    segments = module.split(".") if module else []
    if segments[: len(FORBIDDEN_SEGMENTS)] == FORBIDDEN_SEGMENTS:
        return True
    if relative:
        # `from . import channels` arrives as an empty module with the name in
        # the alias list, which the caller checks separately.
        return not segments or segments[0] == "channels"
    return False


def imports_channels(source: str) -> bool:
    """Whether a module imports from `fittrack.channels`, in any spelling."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(names_channels(alias.name, relative=False) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            relative = bool(node.level)
            # `from fittrack import channels` and `from . import channels` both
            # put the package in the alias list rather than in the module, so
            # the module alone never names it. Joining the two is what catches
            # the most idiomatic absolute spelling of the one import this whole
            # file exists to ban — which the first two versions both missed.
            paths = [f"{module}.{alias.name}" if module else alias.name for alias in node.names]
            if any(names_channels(path, relative=relative) for path in paths):
                return True
            if module and names_channels(module, relative=relative):
                return True
        elif isinstance(node, ast.Call) and imports_channels_dynamically(node):
            return True
    return False


def imports_channels_dynamically(node: ast.Call) -> bool:
    """`importlib.import_module("fittrack.channels...")` and `__import__`.

    An AST check that only looks at `import` statements is evaded by spelling
    the module as a string. Nothing in the codebase imports dynamically, so the
    rule here is absolute: a literal naming the channels package, passed to
    something that imports, is a violation regardless of which import function
    it is — matching on the argument rather than on the callee keeps a future
    wrapper from slipping past.
    """
    callee = node.func
    name = (
        callee.attr
        if isinstance(callee, ast.Attribute)
        else callee.id
        if isinstance(callee, ast.Name)
        else ""
    )
    if name not in {"import_module", "__import__"}:
        return False
    # Keywords as well as positionals: `import_module(name="fittrack.channels")`
    # is the same import wearing a different hat.
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    return any(
        isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and names_channels(argument.value, relative=False)
        for argument in arguments
    )


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


# --------------------------------------------------------------------------- #
# Spellings that got past the first version
# --------------------------------------------------------------------------- #
#
# Found by attacking the checker rather than by reading it. Each of these is a
# way a domain module could reach `channels/` while the guardrail stayed green,
# which makes them the only tests here that matter — a rule nobody tried to
# break is a rule nobody knows the shape of.


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from ..channels.telegram import Adapter", id="relative submodule"),
        pytest.param("from ...channels.telegram.api import send", id="relative deeper"),
        pytest.param("from ..channels import Adapter", id="relative package"),
        pytest.param("from . import channels", id="relative sibling"),
        pytest.param("from fittrack.channels.telegram import Adapter", id="absolute submodule"),
        pytest.param("import fittrack.channels.telegram.api", id="absolute deep"),
        pytest.param(
            'import importlib\nm = importlib.import_module("fittrack.channels.telegram")',
            id="importlib",
        ),
        pytest.param('m = __import__("fittrack.channels")', id="dunder import"),
        pytest.param("from fittrack import channels", id="package as the alias"),
        pytest.param("from fittrack import channels as ch", id="package aliased"),
        pytest.param(
            'm = importlib.import_module(name="fittrack.channels")', id="importlib by keyword"
        ),
        pytest.param('m = __import__(name="fittrack.channels")', id="dunder by keyword"),
    ],
)
def test_the_checker_catches_every_import_spelling(source: str) -> None:
    assert imports_channels(source), f"this reaches channels/ unnoticed:\n{source}"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from fittrack.subchannels import X", id="different package"),
        pytest.param("from ..graph import build", id="a sibling that is not channels"),
        pytest.param("from . import nodes", id="relative sibling that is not channels"),
        pytest.param('NOTE = "channel_caps is read in voice.py"', id="a mention in a string"),
        pytest.param('m = importlib.import_module("fittrack.agents.voice")', id="another module"),
        pytest.param("from fittrack import agents", id="a sibling package as the alias"),
        pytest.param("from fittrack.graph import channels_helper", id="a name that merely starts"),
    ],
)
def test_the_checker_does_not_fire_on_innocent_code(source: str) -> None:
    """A guardrail that cries wolf gets an exception added to it, then another."""
    assert not imports_channels(source), f"false positive on:\n{source}"
