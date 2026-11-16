import pytest

from common.config import ConfigError, Settings, get_bool, get_float, get_int, get_str


def test_get_str_returns_value_stripped():
    assert get_str("NAME", environ={"NAME": "  hello "}) == "hello"


def test_get_str_missing_without_default_raises_with_name():
    with pytest.raises(ConfigError, match="NAME is required"):
        get_str("NAME", environ={})


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_counts_as_unset(raw):
    assert get_str("NAME", "fallback", environ={"NAME": raw}) == "fallback"


def test_get_int_parses_and_checks_range():
    assert get_int("N", environ={"N": "42"}, min_value=1, max_value=100) == 42
    with pytest.raises(ConfigError, match="at least 1"):
        get_int("N", environ={"N": "0"}, min_value=1)
    with pytest.raises(ConfigError, match="at most 3"):
        get_int("N", environ={"N": "4"}, max_value=3)


@pytest.mark.parametrize("raw", ["abc", "1.5", "0x10"])
def test_get_int_rejects_non_integers(raw):
    with pytest.raises(ConfigError, match="N must be an integer"):
        get_int("N", environ={"N": raw})


def test_get_int_default_when_unset():
    assert get_int("N", 7, environ={}) == 7


def test_get_float_parses_and_rejects_non_finite():
    assert get_float("F", environ={"F": "2.5"}) == 2.5
    for raw in ("nan", "inf", "-inf"):
        with pytest.raises(ConfigError, match="finite"):
            get_float("F", environ={"F": raw})
    with pytest.raises(ConfigError, match="must be a number"):
        get_float("F", environ={"F": "far"})


@pytest.mark.parametrize("raw,expected", [("1", True), ("TRUE", True), ("yes", True), ("off", False), ("0", False)])
def test_get_bool(raw, expected):
    assert get_bool("B", environ={"B": raw}) is expected


def test_get_bool_rejects_other_values():
    with pytest.raises(ConfigError, match="true or false"):
        get_bool("B", environ={"B": "maybe"})


def test_settings_defaults_match_api_contract():
    settings = Settings.from_env(environ={})
    assert settings.coordinator_url == "http://coordinator:8000"
    assert settings.rabbitmq_url == "amqp://guest:guest@rabbitmq:5672/"
    assert settings.max_radius_km == 50.0
    assert settings.max_attempts == 5
    assert settings.match_delay_ms == 0
    assert settings.sweep_interval_s == 30
    assert settings.log_level == "INFO"


def test_settings_reads_overrides_and_strips_trailing_slash():
    settings = Settings.from_env(
        environ={
            "COORDINATOR_URL": "http://localhost:9000/",
            "MAX_RADIUS_KM": "12.5",
            "MAX_ATTEMPTS": "3",
            "MATCH_DELAY_MS": "300",
            "LOG_LEVEL": "debug",
        }
    )
    assert settings.coordinator_url == "http://localhost:9000"
    assert settings.max_radius_km == 12.5
    assert settings.max_attempts == 3
    assert settings.match_delay_ms == 300
    assert settings.log_level == "DEBUG"


def test_settings_rejects_bad_values():
    with pytest.raises(ConfigError, match="MAX_ATTEMPTS"):
        Settings.from_env(environ={"MAX_ATTEMPTS": "0"})
