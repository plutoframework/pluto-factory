"""Secret scope and transport tests, plus a real local age round trip."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pluto_factory.config import write_config
from pluto_factory.credentials import (
    CredentialError,
    _environment,
    credential_environment,
    get_secret,
    scaffold_secret_config,
    set_reference,
    set_secret,
)
from pluto_factory.models import FactoryConfig, Repository


def factory(tmp_path: Path) -> Path:
    home = tmp_path / "factory"
    repo = tmp_path / "repo"
    repo.mkdir()
    write_config(
        home,
        FactoryConfig(
            name="credentials-test",
            auth_mode="api_key",
            api_key_env="EXAMPLE_KEY",
            repositories=[Repository(name="app", path=str(repo))],
        ),
    )
    return home


def test_explicit_provider_and_stdin_are_used(tmp_path, monkeypatch):
    home = factory(tmp_path)
    scaffold_secret_config(home, age_recipient="age1testpublicrecipient")
    monkeypatch.setattr(shutil, "which", lambda _: "/test/fnox")
    observed = []

    def invoke(argv, **kwargs):
        observed.append((argv, kwargs))
        with (home / "fnox.toml").open("a") as stream:
            stream.write(
                '\n[secrets.EXAMPLE_KEY]\nprovider = "factory"\n'
                'value = "encrypted-fixture"\n'
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", invoke)
    secret = "example-test-token"
    set_secret(home, "EXAMPLE_KEY", secret)
    argv, options = observed[0]
    assert secret not in argv
    assert options["input"] == secret
    assert argv[-4:] == ["set", "EXAMPLE_KEY", "--provider", "factory"]
    assert options["env"]["FNOX_CONFIG_DIR"] == str(home / "fnox-global")


def test_backend_failure_does_not_expose_output(tmp_path, monkeypatch):
    home = factory(tmp_path)
    scaffold_secret_config(home, age_recipient="age1testpublicrecipient")
    monkeypatch.setattr(shutil, "which", lambda _: "/test/fnox")
    secret = "backend-must-not-expose-this"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout=secret, stderr=secret
        ),
    )
    with pytest.raises(CredentialError) as error:
        set_secret(home, "EXAMPLE_KEY", secret)
    assert secret not in str(error.value)


def test_factory_global_and_profile_are_isolated(tmp_path, monkeypatch):
    home = factory(tmp_path)
    monkeypatch.setenv("FNOX_CONFIG_DIR", "/machine-global")
    monkeypatch.setenv("FNOX_PROFILE", "production")
    monkeypatch.setenv("FNOX_AGE_KEY", "unrelated-private-identity")
    environment = _environment(home)
    assert environment["FNOX_CONFIG_DIR"] == str(home / "fnox-global")
    assert "FNOX_PROFILE" not in environment
    assert "FNOX_AGE_KEY" not in environment
    assert (home / "fnox-global").stat().st_mode & 0o077 == 0


def test_no_plaintext_provider_fallback(tmp_path):
    home = factory(tmp_path)
    (home / "fnox.toml").write_text('[providers.factory]\ntype = "plain"\n')
    with pytest.raises(CredentialError, match="age or 1Password"):
        set_secret(home, "EXAMPLE_KEY", "example-test-token")


def test_plaintext_definition_is_refused(tmp_path):
    home = factory(tmp_path)
    scaffold_secret_config(home, age_recipient="age1testpublicrecipient")
    with (home / "fnox.toml").open("a") as stream:
        stream.write('EXAMPLE_KEY = "plaintext-token"\n')
    with pytest.raises(CredentialError, match="plaintext"):
        get_secret(home, "EXAMPLE_KEY")


def test_1password_reference_does_not_accept_raw_value(tmp_path):
    home = factory(tmp_path)
    scaffold_secret_config(home, "1password", op_vault="Engineering")
    set_reference(home, "EXAMPLE_KEY", "op://Engineering/Factory/token")
    assert "op://Engineering/Factory/token" in (home / "fnox.toml").read_text()
    with pytest.raises(CredentialError, match="item references"):
        set_secret(home, "OTHER_KEY", "example-test-token")


def test_only_declared_api_credential_is_exported(tmp_path, monkeypatch):
    home = factory(tmp_path)
    scaffold_secret_config(home, age_recipient="age1testpublicrecipient")
    monkeypatch.setenv("EXAMPLE_KEY", "selected-key")
    monkeypatch.setenv("UNRELATED_KEY", "not-exported")
    assert credential_environment(home) == {"EXAMPLE_KEY": "selected-key"}


@pytest.mark.skipif(
    not shutil.which("fnox") or not shutil.which("age-keygen"),
    reason="real age/fnox executables are required",
)
def test_real_age_encrypts_and_resolves_without_plaintext_storage(tmp_path):
    home = factory(tmp_path)
    scaffold_secret_config(home, generate_age_key=True)
    token = "disposable-fixture-token-not-a-real-key"
    set_secret(home, "EXAMPLE_KEY", token)
    assert get_secret(home, "EXAMPLE_KEY") == token
    assert credential_environment(home) == {"EXAMPLE_KEY": token}
    assert token not in (home / "fnox.toml").read_text()
    assert not (home / "credentials.json").exists()
    assert (home / "keys" / "age.txt").stat().st_mode & 0o077 == 0


def test_existing_factory_global_config_is_not_loaded(tmp_path):
    home = factory(tmp_path)
    scaffold_secret_config(home, age_recipient="age1testpublicrecipient")
    (home / "fnox-global" / "config.toml").write_text('[secrets]\nKEY="bad"\n')
    with pytest.raises(CredentialError, match="must have no config"):
        _environment(home)
