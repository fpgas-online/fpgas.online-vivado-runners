import importlib.util
import pathlib

SPEC = importlib.util.spec_from_file_location(
    "build_vivado_disk", pathlib.Path(__file__).parents[1] / "image" / "build_vivado_disk.py"
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def test_output_name_carries_version_and_date():
    assert mod.output_name(pathlib.Path("/opt/Xilinx/2025.2"), "2026-10-02") == "vivado-2025.2-2026-10-02.squashfs"


def test_command_keeps_the_version_directory_and_owns_everything_as_root():
    cmd = mod.command(pathlib.Path("/opt/Xilinx/2025.2"), pathlib.Path("/i/out.squashfs"), [])
    assert cmd[:3] == ["mksquashfs", "/opt/Xilinx/2025.2", "/i/out.squashfs"]
    assert "-keep-as-directory" in cmd
    assert "-all-root" in cmd
    assert cmd[cmd.index("-comp") + 1] == "zstd"
    assert "-e" not in cmd


def test_excludes_are_relative_to_the_squashfs_root():
    cmd = mod.command(pathlib.Path("/opt/Xilinx/2025.2"), pathlib.Path("/i/out.squashfs"), ["Vitis", "data/xsim"])
    assert cmd[-3:] == ["-e", "2025.2/Vitis", "2025.2/data/xsim"]


GENERIC = b"INCREMENT ip_free xilinxd 2025.11 permanent uncounted ABCDEF012345 HOSTID=ANY ISSUER=x\n"
NODE_LOCKED = (
    b"INCREMENT synthesis xilinxd 2019.12 permanent uncounted ABCDEF012345 \\\n\tHOSTID=525400aabbcc ISSUER=x\n"
)
FLOATING = b"SERVER licence-host 525400aabbcc 2100\nUSE_SERVER\nINCREMENT synthesis xilinxd 2019.12 permanent 1 ABC\n"


def test_generic_shipped_licences_are_not_machine_tied():
    assert not mod.is_machine_tied(GENERIC)
    assert not mod.is_machine_tied(GENERIC.lower().replace(b"hostid=any", b"HOSTID=any"))
    assert not mod.is_machine_tied(b"# nothing here\n")


def test_node_locked_and_server_licences_are_machine_tied():
    assert mod.is_machine_tied(NODE_LOCKED)
    assert mod.is_machine_tied(FLOATING)
    assert mod.is_machine_tied(GENERIC + NODE_LOCKED)


def test_only_machine_tied_licence_files_are_reported(tmp_path):
    source = tmp_path / "2025.2"
    (source / "data/ip/core_licenses").mkdir(parents=True)
    (source / "data/ip/core_licenses/Xilinx.lic").write_bytes(GENERIC)
    (source / "notes.txt").write_bytes(NODE_LOCKED)  # not a licence file name: never read
    assert mod.machine_tied_licences(source) == []
    (source / "Vivado").mkdir()
    (source / "Vivado/Site.LIC").write_bytes(NODE_LOCKED)
    assert mod.machine_tied_licences(source) == [source / "Vivado/Site.LIC"]


def test_the_build_refuses_a_tree_with_a_machine_tied_licence(tmp_path, monkeypatch, capsys):
    import pytest

    source = tmp_path / "2025.2"
    (source / "Vivado").mkdir(parents=True)
    (source / "Vivado/settings64.sh").write_text("")
    (source / "Vivado/node.lic").write_bytes(NODE_LOCKED)
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr(mod.sys, "argv", ["build_vivado_disk.py", "--source", str(source), "--images-dir", str(images)])
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("mksquashfs must not run"))
    with pytest.raises(SystemExit, match="tied to a machine"):
        mod.main()
    assert list(images.iterdir()) == []
