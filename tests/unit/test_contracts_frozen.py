"""배치 A: 계약(모델·포트·스키마·픽스처)이 서로 맞물리는지 확인한다."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from nsm.domain.models import CardDeck, RenderedPost
from nsm.ports import (
    CardRenderer,
    Classifier,
    Clock,
    CostSink,
    ImageGenerator,
    Notifier,
    PlatformAdapter,
    Source,
    TextGenerator,
)
from nsm.render.placeholder import PlaceholderRenderer
from nsm.store.db import Database, from_db, to_db
from tests.fakes import (
    FakeClassifier,
    FakeCostSink,
    FakeNotifier,
    FakePlatformAdapter,
    FakeRenderer,
    FakeSource,
    FakeTextGenerator,
    FixedClock,
    NullImageGenerator,
    load_comment_cases,
    load_source_items,
)


@pytest.mark.parametrize("template", ["info", "list", "compare"])
def test_sample_decks_follow_card_deck_contract(fixtures_dir: Path, template: str) -> None:
    deck = CardDeck.model_validate_json(
        (fixtures_dir / "decks" / f"{template}.json").read_text("utf-8")
    )
    assert deck.template == template
    assert deck.slides[0].kind == "cover"
    assert deck.slides[-1].kind == "outro"


def test_deck_rejects_wrong_middle_slide(fixtures_dir: Path) -> None:
    raw = json.loads((fixtures_dir / "decks" / "info.json").read_text("utf-8"))
    raw["slides"][1] = {"kind": "list", "heading": "x", "bullets": ["a"]}
    with pytest.raises(ValidationError):
        CardDeck.model_validate(raw)


def test_deck_requires_cover_first(fixtures_dir: Path) -> None:
    raw = json.loads((fixtures_dir / "decks" / "info.json").read_text("utf-8"))
    raw["slides"] = raw["slides"][1:]
    with pytest.raises(ValidationError):
        CardDeck.model_validate(raw)


def test_blog_post_requires_title() -> None:
    with pytest.raises(ValidationError):
        RenderedPost(content_id="c", platform="naver_blog", text="t", images=[])
    RenderedPost(content_id="c", platform="threads", text="t", images=[])


def test_fakes_implement_ports() -> None:
    assert isinstance(FakePlatformAdapter("threads"), PlatformAdapter)
    assert isinstance(FakeSource("s", []), Source)
    assert isinstance(FakeClassifier(), Classifier)
    assert isinstance(FakeTextGenerator(), TextGenerator)
    assert isinstance(NullImageGenerator(), ImageGenerator)
    assert isinstance(FakeRenderer(), CardRenderer)
    assert isinstance(PlaceholderRenderer(), CardRenderer)
    assert isinstance(FakeNotifier(), Notifier)
    assert isinstance(FixedClock(datetime.now(UTC)), Clock)
    assert isinstance(FakeCostSink(), CostSink)


def test_fixture_files_parse() -> None:
    cases = load_comment_cases()
    assert {c for _, c in cases} == {
        "praise_general",
        "question",
        "complaint",
        "price_refund",
        "legal",
        "spam",
    }
    assert len(load_source_items()) >= 3


def test_persona_fixture_is_valid_app_config(app_config) -> None:
    assert app_config.persona.name == "루나"
    assert app_config.settings.dry_run is True
    assert app_config.settings.quotas["threads"] == 2


def test_schema_applies_and_versions(db: Database) -> None:
    assert db.schema_version == 1
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables")}
    assert {"contents", "publications", "comments", "costs", "settings_kv"} <= tables


def test_schema_apply_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "x.duckdb"
    Database(p).close()
    d = Database(p)
    assert d.schema_version == 1
    assert d.execute("SELECT count(*) FROM schema_version")[0][0] == 1
    d.close()


def test_datetime_roundtrip_is_utc() -> None:
    aware = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    assert from_db(to_db(aware)) == aware
    with pytest.raises(ValueError):
        to_db(datetime(2026, 10, 11, 10, 0))


def test_placeholder_renderer_writes_one_png_per_slide(fixtures_dir: Path, tmp_path: Path) -> None:
    deck = CardDeck.model_validate_json((fixtures_dir / "decks" / "info.json").read_text("utf-8"))
    paths = PlaceholderRenderer().render(deck, tmp_path)
    assert len(paths) == len(deck.slides)
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in paths)
