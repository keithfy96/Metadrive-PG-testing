"""`studio` refuses a non-loopback bind. Checked here because the failure is a security failure."""

from __future__ import annotations

import pytest
import typer

from scenariobank.cli import _LOOPBACK, studio


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_a_non_loopback_bind_is_refused_before_anything_is_served(host):
    with pytest.raises(typer.BadParameter) as raised:
        studio(host=host)
    assert "loopback only" in str(raised.value)
    # The reason travels with the refusal: someone reaching for --host wants to know why not.
    assert "no authentication" in str(raised.value)


@pytest.mark.parametrize("host", sorted(_LOOPBACK))
def test_every_loopback_spelling_gets_past_the_guard(host, monkeypatch, tmp_path):
    """The refusal above is only worth having if it does not also reject the valid cases."""
    served: dict[str, object] = {}
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: served.update(kwargs, app=app),
    )
    studio(banks_root=tmp_path, host=host, port=8770)
    assert served["host"] == host
    assert served["port"] == 8770
