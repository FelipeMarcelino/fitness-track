"""Splitting a SQL script into statements asyncpg will accept.

asyncpg prepares every statement, and a prepared statement may hold exactly one
command. The schema of spec 5.2 is one script, and it contains `DO $$ ... $$`
blocks whose bodies are full of semicolons — so splitting on `;` naively would
cut them in half and produce syntax errors that point at the wrong line.
"""

from __future__ import annotations

import pytest

from fittrack.db.sql import split_statements


def test_plain_statements_split_on_the_semicolon() -> None:
    assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_a_trailing_semicolon_produces_no_empty_statement() -> None:
    assert split_statements("SELECT 1;\n\n") == ["SELECT 1"]


def test_a_missing_trailing_semicolon_still_yields_the_statement() -> None:
    assert split_statements("SELECT 1") == ["SELECT 1"]


def test_a_dollar_quoted_body_is_not_split() -> None:
    script = """
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1) THEN
        CREATE ROLE x;
      END IF;
    END $$;
    SELECT 1;
    """
    statements = split_statements(script)
    assert len(statements) == 2
    assert "CREATE ROLE x;" in statements[0]
    assert statements[1] == "SELECT 1"


def test_a_tagged_dollar_quote_is_not_split() -> None:
    """Nested blocks use a tag — `$f$` inside `$$` — and both must survive."""
    script = "DO $$ BEGIN EXECUTE format($f$ SELECT 1; $f$); END $$;\nSELECT 2;"
    statements = split_statements(script)
    assert len(statements) == 2
    assert "$f$ SELECT 1; $f$" in statements[0]


def test_a_semicolon_inside_a_string_literal_is_not_a_boundary() -> None:
    statements = split_statements("SELECT 'a;b'; SELECT 2;")
    assert statements == ["SELECT 'a;b'", "SELECT 2"]


def test_a_semicolon_inside_a_line_comment_is_not_a_boundary() -> None:
    statements = split_statements("SELECT 1 -- a; comment\n; SELECT 2;")
    assert len(statements) == 2
    assert statements[1] == "SELECT 2"


def test_a_semicolon_inside_a_block_comment_is_not_a_boundary() -> None:
    statements = split_statements("SELECT 1 /* a; comment */; SELECT 2;")
    assert len(statements) == 2


def test_comments_between_statements_stay_with_their_statement() -> None:
    statements = split_statements("-- why\nCREATE TABLE t (id int);\nSELECT 1;")
    assert statements[0].startswith("-- why")


def test_an_unterminated_dollar_quote_is_an_error() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        split_statements("DO $$ BEGIN NULL;")


def test_an_empty_script_yields_nothing() -> None:
    assert split_statements("\n  -- nothing here\n") == []


def test_the_real_migration_script_splits() -> None:
    """The case this exists for."""
    from fittrack.db.migrations.versions import _0001_initial_schema as migration

    statements = split_statements(migration.SCHEMA)
    assert len(statements) > 50
    assert any("CREATE TABLE tenant" in s for s in statements)
    assert all(s.count("$$") % 2 == 0 for s in statements)
