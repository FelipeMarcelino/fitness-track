"""Everything under `tests/integration/` needs real services.

Marking by directory instead of by decorator keeps `make test` honest: a new
integration test cannot forget its marker and end up running in the cheap job
(spec section 21.4 orders the cheap gates first).
"""

from __future__ import annotations

from pathlib import Path

import pytest

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook receives the whole session, not just this directory's items.
    for item in items:
        if item.path is not None and item.path.is_relative_to(HERE):
            item.add_marker(pytest.mark.integration)
