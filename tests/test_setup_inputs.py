import pytest

from pluto_factory.setup_inputs import factory_name, repository_choice


def test_human_factory_name_becomes_stable_slug():
    assert factory_name("Snake Game") == "snake-game"
    with pytest.raises(ValueError):
        factory_name("../other")


def test_blank_first_repository_uses_explicit_current_directory(tmp_path):
    assert repository_choice("", tmp_path).endswith(f"={tmp_path}")
    assert repository_choice(f"snake={tmp_path}") == f"snake={tmp_path}"


def test_repository_path_can_be_given_without_a_logical_name(tmp_path):
    root = tmp_path / "Snake Game"
    root.mkdir()
    assert repository_choice(str(root)) == f"snake-game={root}"
