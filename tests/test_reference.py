import pytest
from jmemby.providers.reference import parse_reference


def test_parse_id():
    assert parse_reference("12345") == "12345"


def test_parse_allowlisted_url():
    assert parse_reference("https://example.test/album/42/", ("example.test",)) == "42"


@pytest.mark.parametrize("value", ["0", "abc", "http://example.test/album/42", "https://evil.test/album/42"])
def test_parse_rejects_untrusted(value):
    with pytest.raises(ValueError):
        parse_reference(value, ("example.test",))
