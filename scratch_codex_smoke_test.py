from pathlib import Path


repo_root = Path(__file__).resolve().parent
target = repo_root / "test_records"
if not target.is_dir():
    target = repo_root

count = sum(path.is_file() for path in target.iterdir())
print(f"CODEX_SMOKE_TEST_OK count={count}")
