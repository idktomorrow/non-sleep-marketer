"""경계 2: 플랫폼 어댑터 계약 (S-β, 이슈 #21, contracts.md 10절).

같은 시나리오를 어댑터 픽스처마다 돌린다. 엔진은 어댑터가 드라이런인지 실계정인지 모르므로,
둘 다 이 약속을 지켜야 드라이런에서 본 흐름이 실제와 같다.

- 드라이런 어댑터(threads, instagram): 기본 `pytest`에 포함.
- 실계정 어댑터의 화면 동작은 Aside를 거치지 않는 가짜 실행기로 같은 약속(숨기기 미지원, health)을 확인한다.
- 실계정 실제 호출은 맨 아래 `contract` 마커 케이스(읽기 전용)와 `test_threads_live.py`·`test_instagram_live.py`
  (게시 → 확인 → 삭제)로만 돈다: `uv run pytest -m contract tests/contracts/`.

세부 동작(미리보기 파일 모양, 가짜 댓글 순서 등)은 `test_dryrun_adapter.py`가 본다. 이 파일은 계약만 본다.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

from nsm.adapters.drivers import find_aside_cli
from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.platforms import PLATFORM_CAPABILITIES
from nsm.adapters.platforms.dryrun import DryRunAdapter
from nsm.adapters.platforms.instagram import InstagramAdapter
from nsm.adapters.platforms.threads import ThreadsAdapter
from nsm.domain.labels import VIRTUAL_PERSON_TEXT
from nsm.domain.models import Comment, HealthStatus, PostMetrics, PublishResult, RenderedPost
from nsm.ports import CAPABILITIES, NotSupportedError, PlatformAdapter

ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ["threads", "instagram"]  # 네이버 블로그는 게시 범위 밖(ADR 0016)

# 지표 가용성 매트릭스(contracts.md 10절 표, 실계정 어댑터가 화면에서 읽는 필드).
# 여기 없는 필드는 그 플랫폼에서 늘 None이어야 한다(없는 값을 0으로 채우면 리포트가 거짓말을 한다).
METRIC_FIELDS: dict[str, frozenset[str]] = {
    "instagram": frozenset(
        {"views", "reach", "impressions", "likes", "comments", "saves", "shares"}
    ),
    "threads": frozenset({"views", "reach", "likes", "comments", "shares"}),  # 저장 수 없음
}
ALL_METRIC_FIELDS = frozenset(
    {"views", "reach", "impressions", "likes", "comments", "saves", "shares"}
)
# 드라이런은 리포트를 미리 볼 수 있게 이만큼은 반드시 채운다
DRYRUN_REQUIRED = frozenset({"views", "likes", "comments"})


def _post(media: Path, platform: str) -> RenderedPost:
    img = media / "cards" / "c1" / "slide-01.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG")
    text = f"{VIRTUAL_PERSON_TEXT}\n\n계약 테스트 본문입니다\n#치이카와"
    return RenderedPost(content_id="c1", platform=platform, text=text, images=[img])


# ---------------------------------------------------------------- 드라이런 어댑터


@pytest.fixture(params=PLATFORMS)
def dry(request, tmp_path: Path, clock) -> DryRunAdapter:
    return DryRunAdapter(request.param, tmp_path / "preview", clock)


def _published(dry: DryRunAdapter, tmp_path: Path) -> PublishResult:
    return dry.publish(_post(tmp_path / "media", dry.platform))


def test_implements_port_with_declared_capabilities(dry: DryRunAdapter) -> None:
    assert isinstance(dry, PlatformAdapter)
    caps = dry.capabilities()
    assert caps == set(PLATFORM_CAPABILITIES[dry.platform]) and caps <= CAPABILITIES
    assert {"publish", "fetch_comments", "reply"} <= caps  # 게시·댓글·답글은 모든 플랫폼 필수


def test_health_reports_logged_in(dry: DryRunAdapter) -> None:
    h = dry.health()
    assert isinstance(h, HealthStatus) and h.logged_in and h.detail


def test_publish_returns_id_and_url(dry: DryRunAdapter, tmp_path: Path) -> None:
    res = _published(dry, tmp_path)
    assert isinstance(res, PublishResult)
    assert res.external_id and res.url
    assert re.match(r"^(https?|file):", res.url)  # 대시보드 "열기" 링크로 쓴다
    assert "/" not in res.external_id  # 대시보드 경로 조각으로 쓴다


def test_fetch_new_comments_respects_since(dry: DryRunAdapter, tmp_path: Path, clock) -> None:
    ext = _published(dry, tmp_path).external_id
    assert dry.fetch_new_comments([ext], None) == []  # 막 올린 글
    clock.advance(hours=1)
    first = dry.fetch_new_comments([ext], None)
    assert first, "시간이 지나면 댓글이 온다"
    for c in first:
        assert isinstance(c, Comment)
        assert c.platform == dry.platform and c.post_external_id == ext
        assert c.external_id and "/" not in c.external_id
        assert c.created_at.tzinfo is not None and c.created_at <= clock.now()
    since = max(c.created_at for c in first)
    clock.advance(hours=3)
    later = dry.fetch_new_comments([ext], since)
    assert later and all(c.created_at > since for c in later)  # since 이후만
    assert not {c.external_id for c in later} & {c.external_id for c in first}
    assert dry.fetch_new_comments(["모르는-글"], None) == []  # 없는 글은 빈 목록(예외 아님)


def test_reply_returns_str(dry: DryRunAdapter, tmp_path: Path, clock) -> None:
    ext = _published(dry, tmp_path).external_id
    clock.advance(hours=1)
    c = dry.fetch_new_comments([ext], None)[0]
    assert isinstance(dry.reply(c, "고마워요!"), str)  # 답글 ID를 못 얻으면 빈 문자열도 허용


def test_hide_comment_follows_capability(dry: DryRunAdapter, tmp_path: Path, clock) -> None:
    ext = _published(dry, tmp_path).external_id
    clock.advance(hours=1)
    c = dry.fetch_new_comments([ext], None)[0]
    if "hide_comment" in dry.capabilities():
        dry.hide_comment(c)
    else:
        with pytest.raises(NotSupportedError):  # 엔진은 이걸 보고 사람 검토로 넘긴다
            dry.hide_comment(c)


def test_metrics_follow_availability_matrix(dry: DryRunAdapter, tmp_path: Path, clock) -> None:
    ext = _published(dry, tmp_path).external_id
    clock.advance(hours=24)
    m = dry.read_post_metrics(ext)
    assert isinstance(m, PostMetrics)
    assert m.platform == dry.platform and m.post_external_id == ext
    assert m.captured_at == clock.now()
    filled = {f for f in ALL_METRIC_FIELDS if getattr(m, f) is not None}
    assert filled <= METRIC_FIELDS[dry.platform], (
        f"없는 지표를 채움: {filled - METRIC_FIELDS[dry.platform]}"
    )
    assert filled >= DRYRUN_REQUIRED
    assert all(getattr(m, f) >= 0 for f in filled)
    acc = dry.read_account_metrics()
    assert acc.platform == dry.platform and acc.followers is not None and acc.followers >= 0


def test_metric_matrix_matches_capabilities() -> None:
    """매트릭스는 지표를 읽을 수 있다고 선언한 플랫폼에만 있다."""
    for platform in PLATFORMS:
        assert "post_metrics" in PLATFORM_CAPABILITIES[platform]
        assert METRIC_FIELDS[platform] <= ALL_METRIC_FIELDS
        assert {"views", "likes", "comments"} <= METRIC_FIELDS[platform]


# ------------------------------------------- 실계정 어댑터: 가짜 실행기로 같은 약속 확인


class FakeRepl:
    """Aside를 부르지 않는 실행기. 정해진 값을 돌려주거나 오류를 낸다."""

    def __init__(self, data=None, error: AsideError | None = None) -> None:
        self.data, self.error = data, error
        self.calls = 0

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


LIVE = {"threads": ThreadsAdapter, "instagram": InstagramAdapter}


def _live(platform: str, clock, data=None):
    return LIVE[platform]("@nonsleepmarketer", clock, repl=FakeRepl(data))


@pytest.mark.parametrize("platform", PLATFORMS)
def test_live_adapter_declares_same_capabilities(platform: str, clock) -> None:
    a = _live(platform, clock)
    assert isinstance(a, PlatformAdapter)
    assert a.capabilities() == DryRunAdapter(platform, Path("."), clock).capabilities()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_live_hide_comment_follows_capability(platform: str, clock) -> None:
    a = _live(platform, clock)
    if "hide_comment" in a.capabilities():
        return  # 숨기기 실제 동작은 test_threads_live.py(다른 계정 댓글 필요)
    c = Comment(
        platform=platform,
        external_id="fan:C1",
        post_external_id="P1",
        author_id="fan",
        author_name="fan",
        text="스팸",
        created_at=clock.now() - timedelta(minutes=5),
    )
    with pytest.raises(NotSupportedError):
        a.hide_comment(c)
    assert a.repl.calls == 0  # 화면을 건드리기 전에 막는다


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    ("me", "logged_in", "detail"),
    [
        ("nonsleepmarketer", True, "@nonsleepmarketer"),
        ("", False, "로그인 필요"),
        ("someone_else", False, "≠ 설정 @nonsleepmarketer"),
    ],
)
def test_live_health_scenarios(platform: str, me: str, logged_in: bool, detail: str, clock) -> None:
    """health: 로그인됨 / 로그아웃 / 다른 계정. 다른 계정이면 게시하지 않도록 logged_in=False."""
    h = _live(platform, clock, data={"me": me}).health()
    assert h.logged_in is logged_in and detail in h.detail


# ---------------------------------------------------------------- 실계정(읽기 전용, -m contract)


def _account() -> str:
    text = (ROOT / "docs" / "day1-verification.md").read_text(encoding="utf-8")
    m = re.search(r"^\| 스레드 \| (@[\w.]+)", text, re.M)
    assert m, "day1-verification.md에 테스트 계정이 없음"
    return m.group(1)


live = pytest.mark.skipif(find_aside_cli() is None, reason="Aside CLI 없음")


@pytest.fixture(scope="module", params=PLATFORMS)
def real(request, tmp_path_factory):
    from nsm.domain.clock import SystemClock

    media = tmp_path_factory.mktemp("media")
    return LIVE[request.param](_account(), SystemClock(), media_dir=media)


@pytest.mark.contract
@live
def test_real_health_logged_in_as_test_account(real) -> None:
    h = real.health()
    assert h.logged_in, h.detail
    assert h.detail == _account()


@pytest.mark.contract
@live
def test_real_account_metrics(real) -> None:
    assert real.read_account_metrics().followers is not None


@pytest.mark.contract
@live
def test_real_metrics_follow_availability_matrix(real) -> None:
    """이미 올라간 글 하나로 확인한다(게시하지 않음): NSM_CONTRACT_<PLATFORM>_POST=글 코드."""
    code = os.environ.get(f"NSM_CONTRACT_{real.platform.upper()}_POST")
    if not code:
        pytest.skip(f"NSM_CONTRACT_{real.platform.upper()}_POST 없음")
    m = real.read_post_metrics(code)
    filled = {f for f in ALL_METRIC_FIELDS if getattr(m, f) is not None}
    assert filled <= METRIC_FIELDS[real.platform]
    assert m.comments is not None
    assert isinstance(real.fetch_new_comments([code], None), list)
