from pathlib import Path

from qd_service_control.observe.files import probe_file


def test_missing_file(tmp_path: Path) -> None:
    assert not probe_file(str(tmp_path / "nope"), non_empty=False)


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_text("")
    assert probe_file(str(path), non_empty=False)
    assert not probe_file(str(path), non_empty=True)


def test_non_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "full"
    path.write_text("x")
    assert probe_file(str(path), non_empty=True)
