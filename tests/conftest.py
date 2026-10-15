from pathlib import Path


def pytest_configure() -> None:
    # pytest creates --basetemp with mkdir, not mkdir -p
    Path(".scratch/pytest").mkdir(parents=True, exist_ok=True)
