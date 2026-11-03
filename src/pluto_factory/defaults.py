"""Read secret-free master defaults without changing existing factories."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .models import FactoryConfig


class SecretDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["age", "1password"] = "age"
    age_recipient: str | None = None
    age_key_file: str | None = None
    generate_age_key: bool = False
    op_vault: str | None = None
    op_account: str | None = None

    @model_validator(mode="after")
    def validate_age_selection(self):
        if self.generate_age_key and (self.age_key_file or self.age_recipient):
            raise ValueError("choose an existing age identity or generate one")
        if self.age_recipient and "AGE-SECRET-KEY" in self.age_recipient:
            raise ValueError("age recipients must be public")
        return self


def master_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "pluto" / "pluto.toml"


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def read_defaults(path: Path | None = None) -> dict:
    selected = (path or master_path()).expanduser().resolve()
    document = {}
    if selected.exists():
        with selected.open("rb") as stream:
            document = tomllib.load(stream)
    elif path is not None:
        raise ValueError("the explicit master defaults file does not exist")
    factory = document.get("factory", {})
    if not isinstance(factory, dict):
        raise TypeError("master [factory] defaults must be a table")
    factory = dict(factory)
    try:
        secrets = SecretDefaults.model_validate(factory.pop("secrets", {}))
    except ValidationError as error:
        raise ValueError(
            "master factory secret defaults are invalid"
        ) from error
    if {"name", "repositories"} & factory.keys():
        raise ValueError(
            "master defaults cannot register factory repositories or names"
        )
    try:
        validated = FactoryConfig.model_validate(
            {
                "name": "defaults",
                "repositories": [{"name": "defaults", "path": "."}],
                **factory,
            }
        )
    except ValidationError as error:
        raise ValueError("master factory defaults are invalid") from error
    secret_values = secrets.model_dump(exclude_none=True)
    if "age_key_file" in secret_values:
        key_file = Path(secret_values["age_key_file"]).expanduser()
        if not key_file.is_absolute():
            key_file = selected.parent / key_file
        secret_values["age_key_file"] = str(key_file.resolve())
    return {
        "path": str(selected),
        "exists": selected.is_file(),
        "factory": validated.model_dump(exclude={"name", "repositories"}),
        "secrets": secret_values,
    }
