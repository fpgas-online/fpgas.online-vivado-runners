import vivado_runners


def test_version_is_a_string():
    assert isinstance(vivado_runners.__version__, str)
