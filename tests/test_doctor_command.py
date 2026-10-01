"""talos doctor: talos-doctor is its own product; Talos runs it, finding it beside its own repository."""

import pytest

from talos import cli


@pytest.mark.parametrize("remote, url", [
    ("https://github.com/someone/talos-core.git\n", "https://github.com/someone/talos-doctor"),
    ("https://github.com/someone/talos-core", "https://github.com/someone/talos-doctor"),
    ("git@github.com:someone/talos.git", "https://github.com/someone/talos-doctor"),
    ("", None), ("/a/local/path", None),
])
def test_the_doctor_is_found_beside_this_repository(remote, url):
    assert cli.doctor_url(remote) == url


def test_talos_doctor_passes_its_arguments_and_the_code_folder_through(monkeypatch):
    import sys
    import types

    seen = {}
    fake = types.ModuleType("talos_doctor.cli")
    fake.main = lambda argv: seen.setdefault("argv", argv) and 0
    monkeypatch.setitem(sys.modules, "talos_doctor", types.ModuleType("talos_doctor"))
    monkeypatch.setitem(sys.modules, "talos_doctor.cli", fake)
    sys.modules["talos_doctor"].cli = fake
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor", "--only", "accounts"])
    assert e.value.code == 0
    assert seen["argv"][0] == "--repo" and seen["argv"][2:] == ["--only", "accounts"]
