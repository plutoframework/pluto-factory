"""Per-factory fnox credentials, without plaintext fallback or secret argv."""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path


class CredentialError(RuntimeError):
    """A safe error that never includes backend output or secret values."""


def _reference(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise CredentialError(
            "credential reference must be an environment name"
        )
    return value


def _environment(home: Path) -> dict[str, str]:
    global_home = home / "fnox-global"
    if global_home.is_symlink():
        raise CredentialError("factory fnox directory must not be a symlink")
    global_home.mkdir(mode=0o700, exist_ok=True)
    global_home.chmod(0o700)
    if (global_home / "config.toml").exists():
        raise CredentialError(
            "factory fnox global directory must have no config"
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FNOX_")
    }
    environment["FNOX_CONFIG_DIR"] = str(global_home.resolve())
    environment["FNOX_IF_MISSING"] = "error"
    return environment


def _configuration(home: Path) -> dict:
    path = home / "fnox.toml"
    if path.is_symlink() or not path.is_file():
        raise CredentialError("configure this factory's fnox.toml first")
    try:
        data = tomllib.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise CredentialError(
            "factory fnox configuration is unreadable"
        ) from error
    provider = data.get("providers", {}).get("factory", {})
    if provider.get("type") not in {"age", "1password"}:
        raise CredentialError(
            "factory secrets require an age or 1Password provider"
        )
    if data.get("import") or data.get("profiles"):
        raise CredentialError(
            "factory fnox imports and profile overrides are not supported"
        )
    for definition in data.get("secrets", {}).values():
        if not isinstance(definition, dict):
            raise CredentialError(
                "plaintext secret definitions are not allowed"
            )
        selected = definition.get("provider", data.get("default_provider"))
        if selected != "factory" or "default" in definition:
            raise CredentialError(
                "factory secrets must use the configured provider"
            )
    return data


def _invoke(home: Path, arguments: list[str], value: str | None = None) -> str:
    binary = shutil.which("fnox")
    if not binary:
        raise CredentialError("fnox is not installed")
    try:
        result = subprocess.run(
            [
                binary,
                "-c",
                str((home / "fnox.toml").resolve()),
                "-P",
                "default",
                "--no-color",
                *arguments,
            ],
            cwd=home,
            env=_environment(home),
            input=value,
            stdin=subprocess.DEVNULL if value is None else None,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CredentialError(
            "fnox credential operation did not complete"
        ) from error
    if result.returncode:
        raise CredentialError(
            f"fnox credential operation failed (exit {result.returncode}); "
            "check the selected provider's authentication and configuration"
        )
    return result.stdout.rstrip("\r\n")


def scaffold_secret_config(
    home: Path,
    provider: str = "age",
    *,
    age_recipient: str | None = None,
    age_key_file: Path | None = None,
    generate_age_key: bool = False,
    op_vault: str | None = None,
    op_account: str | None = None,
) -> dict:
    """Create an explicit backend configuration after setup confirmation."""
    home = home.resolve()
    destination = home / "fnox.toml"
    if destination.exists() or destination.is_symlink():
        raise CredentialError("factory fnox configuration already exists")
    if provider not in {"age", "1password"}:
        raise CredentialError("choose age or 1Password for factory secrets")
    _environment(home)
    lines = ['default_provider = "factory"', 'if_missing = "error"', ""]
    lines.extend(["[providers.factory]", f"type = {json.dumps(provider)}"])
    if provider == "age":
        if generate_age_key:
            if age_key_file or age_recipient:
                raise CredentialError(
                    "choose an existing age identity or generate one"
                )
            binary = shutil.which("age-keygen")
            if not binary:
                raise CredentialError(
                    "age-keygen is required to generate an identity"
                )
            keys = home / "keys"
            keys.mkdir(mode=0o700, exist_ok=True)
            age_key_file = keys / "age.txt"
            result = subprocess.run(
                [binary, "-o", str(age_key_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise CredentialError(
                    "could not generate the factory age identity"
                )
            age_key_file.chmod(0o600)
        if age_key_file:
            age_key_file = age_key_file.expanduser().resolve(strict=True)
            if age_key_file.stat().st_mode & 0o077:
                raise CredentialError(
                    "age identity file must be private to its owner"
                )
            if not age_recipient:
                binary = shutil.which("age-keygen")
                if not binary:
                    raise CredentialError(
                        "supply --age-recipient or install age-keygen"
                    )
                result = subprocess.run(
                    [binary, "-y", str(age_key_file)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    raise CredentialError(
                        "could not derive the age public recipient"
                    )
                age_recipient = result.stdout.strip()
            lines.append(f"key_file = {json.dumps(str(age_key_file))}")
        recipients = [age_recipient] if age_recipient else []
        if any("AGE-SECRET-KEY" in recipient for recipient in recipients):
            raise CredentialError("age recipients must be public keys")
        lines.append(f"recipients = {json.dumps(recipients)}")
    else:
        if op_vault:
            lines.append(f"vault = {json.dumps(op_vault)}")
        if op_account:
            lines.append(f"account = {json.dumps(op_account)}")
    lines.extend(["", "[secrets]", ""])
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(lines))
        stream.flush()
        os.fsync(stream.fileno())
    return secret_readiness(home)


def set_secret(home: Path, key: str, value: str) -> None:
    _reference(key)
    data = _configuration(home)
    provider = data["providers"]["factory"]
    if provider["type"] != "age":
        raise CredentialError(
            "1Password uses item references, not raw secret values"
        )
    if not provider.get("recipients"):
        raise CredentialError(
            "configure an age recipient before storing secrets"
        )
    if (
        not value
        or len(value) > 8192
        or any(character.isspace() for character in value)
    ):
        raise CredentialError(
            "credential must be a nonempty token without whitespace"
        )
    _invoke(home, ["set", key, "--provider", "factory"], value=value)
    stored = _configuration(home).get("secrets", {}).get(key, {})
    if stored.get("provider") != "factory" or stored.get("value") == value:
        raise CredentialError(
            "fnox did not record an encrypted provider-backed value"
        )


def set_reference(home: Path, key: str, reference: str) -> None:
    _reference(key)
    data = _configuration(home)
    if data["providers"]["factory"]["type"] != "1password":
        raise CredentialError("item references require the 1Password backend")
    if not reference.startswith("op://") or any(c in reference for c in "\r\n"):
        raise CredentialError("use a full op://vault/item/field reference")
    if key in data.get("secrets", {}):
        raise CredentialError(
            "credential reference already exists; edit fnox.toml"
        )
    with (home / "fnox.toml").open("a") as stream:
        stream.write(
            f"\n[secrets.{json.dumps(key)}]\n"
            'provider = "factory"\n'
            f"value = {json.dumps(reference)}\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def get_secret(home: Path, key: str) -> str:
    _reference(key)
    data = _configuration(home)
    if key not in data.get("secrets", {}):
        raise CredentialError(
            "the requested credential is not registered in this factory"
        )
    return _invoke(home, ["get", key])


def credential_environment(home: Path) -> dict[str, str]:
    """Return only the API key explicitly selected by the factory."""
    from .config import load_config

    config = load_config(home)
    if config.auth_mode != "api_key":
        return {}
    key = _reference(config.api_key_env)
    data = _configuration(home)
    if key in data.get("secrets", {}):
        return {key: get_secret(home, key)}
    if os.environ.get(key):
        return {key: os.environ[key]}
    raise CredentialError("the factory API credential is not configured")


def secret_readiness(home: Path, key: str | None = None) -> dict:
    """Inspect metadata without printing, fetching, or decrypting a secret."""
    data = _configuration(home)
    provider = data["providers"]["factory"]
    kind = provider["type"]
    configured = bool(shutil.which("fnox"))
    if kind == "age":
        identity = provider.get("key_file")
        configured = (
            configured
            and bool(provider.get("recipients"))
            and bool(identity and Path(identity).expanduser().is_file())
        )
    else:
        configured = configured and bool(shutil.which("op"))
    registered = key in data.get("secrets", {}) if key else False
    return {
        "provider": kind,
        "configured": configured,
        "credential_registered": registered,
        "credential_available": (registered and configured)
        or bool(key and os.environ.get(key)),
        "verified": False,
    }
