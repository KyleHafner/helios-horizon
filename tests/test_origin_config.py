import pytest

from game_control.origin_config import PublicOriginConfigError, load_public_origin


def test_load_public_origin_requires_explicit_https_origin():
    assert load_public_origin({"HORIZON_PUBLIC_ORIGIN": "https://console.example"}) == "https://console.example"


@pytest.mark.parametrize("value", [None, "", " https://console.example", "https://console.example/", "http://console.example", "https://console.example/path", "https://user:pass@console.example", "https://console.example:bad", "https://[not-an-ipv6]", "https://[invalid", "https://console.example\n", "https://cönsole.example"])
def test_load_public_origin_rejects_unsafe_or_implicit_values(value):
    environment = {} if value is None else {"HORIZON_PUBLIC_ORIGIN": value}
    with pytest.raises(PublicOriginConfigError, match="HORIZON_PUBLIC_ORIGIN"):
        load_public_origin(environment)


def test_load_public_origin_allows_explicit_origin_port():
    assert load_public_origin({"HORIZON_PUBLIC_ORIGIN": "https://console.example:8444"}) == "https://console.example:8444"
