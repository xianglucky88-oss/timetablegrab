import pytest

def test_diff_is_open_gap():
    from src.timetablegrab import diff
    with pytest.raises(NotImplementedError):
        diff.process({"id": "x"})

def test_validate_is_open_gap():
    from src.timetablegrab import validate
    with pytest.raises(NotImplementedError):
        validate.process({"id": "x"})
