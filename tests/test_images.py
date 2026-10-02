import pytest

from vivado_runners import images


def make(tmp_path, *names):
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    return tmp_path


def test_resolve_follows_both_symlinks(tmp_path):
    d = make(tmp_path, "runner-base-2026-10-02.1.qcow2", "vivado-2025.2-2026-10-02.squashfs")
    (d / "runner-base-current").symlink_to("runner-base-2026-10-02.1.qcow2")
    (d / "vivado-current").symlink_to("vivado-2025.2-2026-10-02.squashfs")
    v = images.resolve(d)
    assert v.base == d / "runner-base-2026-10-02.1.qcow2"
    assert v.vivado == d / "vivado-2025.2-2026-10-02.squashfs"


def test_resolve_rejects_missing_dangling_and_escaping_links(tmp_path):
    d = make(tmp_path, "vivado-2025.2-2026-10-02.squashfs")
    (d / "vivado-current").symlink_to("vivado-2025.2-2026-10-02.squashfs")
    with pytest.raises(images.ImageError, match="runner-base-current is not a symlink"):
        images.resolve(d)
    (d / "runner-base-current").symlink_to("runner-base-gone.qcow2")
    with pytest.raises(images.ImageError, match="does not exist"):
        images.resolve(d)
    (d / "runner-base-current").unlink()
    outside = tmp_path.parent / "elsewhere.qcow2"
    outside.write_bytes(b"x")
    (d / "runner-base-current").symlink_to(outside)
    with pytest.raises(images.ImageError, match="outside"):
        images.resolve(d)


def test_activate_repoints_atomically_and_rolls_back(tmp_path):
    d = make(tmp_path, "runner-base-2026-09-25.1.qcow2", "runner-base-2026-10-02.1.qcow2")
    images.activate(d, "base", "runner-base-2026-10-02.1.qcow2")
    assert images.current(d, "base") == d / "runner-base-2026-10-02.1.qcow2"
    images.activate(d, "base", "runner-base-2026-09-25.1.qcow2")
    assert images.current(d, "base") == d / "runner-base-2026-09-25.1.qcow2"
    assert sorted(p.name for p in d.iterdir() if p.name.startswith(".")) == []


def test_activate_refuses_unknown_or_wrong_kind(tmp_path):
    d = make(tmp_path, "vivado-2025.2-2026-10-02.squashfs")
    with pytest.raises(images.ImageError, match="no such base image"):
        images.activate(d, "base", "runner-base-2026-01-01.1.qcow2")
    with pytest.raises(images.ImageError, match="no such base image"):
        images.activate(d, "base", "vivado-2025.2-2026-10-02.squashfs")


def test_list_versions_is_sorted_and_skips_the_symlink(tmp_path):
    d = make(tmp_path, "runner-base-2026-10-02.1.qcow2", "runner-base-2026-09-25.1.qcow2")
    (d / "runner-base-current").symlink_to("runner-base-2026-10-02.1.qcow2")
    assert [p.name for p in images.list_versions(d, "base")] == [
        "runner-base-2026-09-25.1.qcow2",
        "runner-base-2026-10-02.1.qcow2",
    ]


def test_current_is_none_without_a_link(tmp_path):
    assert images.current(tmp_path, "base") is None


def test_next_base_name_counts_builds_per_day(tmp_path):
    assert images.next_base_name(tmp_path, "2026-10-02") == "runner-base-2026-10-02.1.qcow2"
    make(tmp_path, "runner-base-2026-10-02.1.qcow2", "runner-base-2026-10-02.2.qcow2")
    assert images.next_base_name(tmp_path, "2026-10-02") == "runner-base-2026-10-02.3.qcow2"
