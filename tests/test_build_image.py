import importlib.util
import pathlib

import pytest

SPEC = importlib.util.spec_from_file_location(
    "build_image", pathlib.Path(__file__).parents[1] / "image" / "build_image.py"
)
build_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_image)


def test_user_data_runs_provision_from_the_seed_and_powers_off():
    text = build_image.user_data()
    assert text.startswith("#cloud-config\n")
    assert "mount -o ro /dev/disk/by-label/cidata /mnt/payload" in text
    assert "python3 /mnt/payload/provision.py" in text
    assert "mode: poweroff" in text
    assert "condition: true" in text


def test_expected_sha512_reads_the_right_line():
    sums = "aaa  debian-13-genericcloud-amd64.qcow2\nbbb  debian-13-generic-amd64.qcow2\n"
    assert build_image.expected_sha512(sums, "debian-13-generic-amd64.qcow2") == "bbb"
    with pytest.raises(SystemExit, match="not listed"):
        build_image.expected_sha512(sums, "missing.qcow2")


def test_payload_lists_every_file_provision_reads(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text("lock")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\n")
    seed = tmp_path / "seed"
    build_image.write_seed(seed, lock, pyproject, "bbb")
    assert sorted(p.name for p in seed.iterdir()) == [
        "debian-image.sha512",
        "meta-data",
        "provision.py",
        "pyproject.toml",
        "user-data",
        "uv.lock",
        "versions.toml",
        "vivado-runner-boot",
        "vivado-runner.service",
    ]
    assert (seed / "debian-image.sha512").read_text() == "bbb\n"
    assert (seed / "uv.lock").read_text() == "lock"
