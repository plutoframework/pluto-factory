"""A settled report or open PR must not unlock dependent implementation."""

from pluto_factory.config import write_config
from pluto_factory.engine import Engine
from pluto_factory.models import FactoryConfig, Repository, Submission


def test_out_of_order_dependency_dispatch_waits_for_merged_completion(tmp_path):
    home = tmp_path / "factory"
    write_config(
        home,
        FactoryConfig(
            name="dependency-dispatch",
            harness="fixture",
            coordinator="rules",
            isolation="cooperative",
            repositories=[
                Repository(name="app", path=str(tmp_path), github="owner/snake")
            ],
        ),
    )
    engine = Engine(home)
    try:

        def issue(number, dependencies=()):
            return engine.state.submit(
                Submission(
                    title=f"Increment {number}",
                    instruction="Implement this small increment",
                    acceptance=["This increment passes its checks"],
                    repositories=["app"],
                    source="github:owner/snake",
                    delivery_key=str(number),
                    depends_on_sources=list(dependencies),
                )
            )

        child = issue(2, ["github:owner/snake:1"])
        assert not engine._ready(child)
        assert not engine.tick()
        parent = issue(1)
        observed = []
        engine._step = lambda work: observed.append(work["id"])
        assert engine.tick()
        assert observed == [parent["id"]]
        parent = engine.state.update_work(
            parent["id"], phase="awaiting_merge", control="backoff"
        )
        assert not engine._ready(child)
        assert engine._ready(parent)
        engine.state.update_work(
            parent["id"], phase="completed", control="settled"
        )
        assert engine.tick()
        assert observed == [parent["id"], child["id"]]
    finally:
        engine.state.close()
