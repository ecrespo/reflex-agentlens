"""Property suites reuse the contract database fixtures (SQLite always, PostgreSQL when set)."""

from tests.contract.conftest import db, sqlite_template  # noqa: F401 - re-exported fixtures
