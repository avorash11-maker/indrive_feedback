import importlib.util
from pathlib import Path


def _load_root_main_module():
    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("repo_root_main", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_root_main_defaults_to_competitor_tracker(monkeypatch):
    module = _load_root_main_module()
    captured = {}

    monkeypatch.setattr(module, "competitor_tracker_main", lambda args=None: captured.setdefault("args", args))
    monkeypatch.setattr(module, "legacy_indrive_media_main", lambda: captured.setdefault("legacy", True))

    module.main(["run", "--days", "7"])

    assert captured == {"args": ["run", "--days", "7"]}


def test_root_main_routes_legacy_only_when_explicit(monkeypatch):
    module = _load_root_main_module()
    captured = {}

    monkeypatch.setattr(module, "competitor_tracker_main", lambda args=None: captured.setdefault("args", args))
    monkeypatch.setattr(module, "legacy_indrive_media_main", lambda: captured.setdefault("legacy", True))

    module.main(["legacy-indrive-media", "--days", "30"])

    assert captured == {"legacy": True}
