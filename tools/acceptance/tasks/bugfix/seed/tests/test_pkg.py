import pytest
from pkg.intervals import merge
from pkg.slug import slugify
from pkg.duration import parse

def test_merge_touching_and_adjacent():
    assert merge([[1, 3], [3, 5]]) == [[1, 5]]
    assert merge([[1, 3], [4, 5]]) == [[1, 5]]
    assert merge([[5, 6], [1, 2]]) == [[1, 2], [5, 6]]
    assert merge([]) == []

def test_slugify_collapses_and_trims():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  a   b  ") == "a-b"
    assert slugify("--x--") == "x"
    assert slugify("") == ""

def test_parse_duration():
    assert parse("1h30m") == 5400
    assert parse("45m") == 2700
    assert parse("90s") == 90
    assert parse("1h2m3s") == 3723
    with pytest.raises(ValueError):
        parse("")
    with pytest.raises(ValueError):
        parse("3x")
