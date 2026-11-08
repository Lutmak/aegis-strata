import os, sys, pathlib, pytest
sys.path.insert(0, os.environ.get("WORK", str(pathlib.Path(__file__).parent.parent)))
from pkg.intervals import merge
from pkg.slug import slugify
from pkg.duration import parse
def test_merge_more():
    assert merge([[1, 10], [2, 3], [12, 12], [11, 11]]) == [[1, 12]]
    assert merge([[1, 1]]) == [[1, 1]]
def test_slug_more():
    assert slugify("Ünïcödé & ASCII") == "n-c-d-ascii"
    assert slugify("2024-01-02") == "2024-01-02"
def test_duration_more():
    assert parse("2h") == 7200
    assert parse("0m") == 0
    with pytest.raises(ValueError):
        parse("30m1h")
