"""브라우저 드라이버: 플랫폼 어댑터·수집 소스가 쓰는 얇은 브라우저 조작 층 (ADR 0002).

포트가 아니라 어댑터 내부 세부사항이다. 수집에 필요한 최소 동작만 정의하고,
게시에 필요한 동작(click, fill, upload)은 배치 G에서 추가한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    context: str  # 링크를 감싼 블록의 텍스트(요약문 추출용, 최대 400자)


class BrowserDriver(Protocol):
    def goto(self, url: str) -> None: ...
    def links(self) -> list[Link]: ...
    def page_text(self) -> str: ...
    def close(self) -> None: ...


class DriverUnavailableError(RuntimeError):
    pass


# 링크를 감싼 블록의 텍스트(요약문) + 그 결과 항목의 날짜 표기(S16). 네이버 새 결과 화면은 날짜("2시간 전")가
# 링크 블록이 아니라 두세 단계 위 항목 칸에 있어 요약문만으로는 공개 시각을 못 읽었다(실검증에서 전부 "날짜 확인 필요").
# 목록(UL·OL)이나 날짜가 둘 이상인 칸(이웃 결과 포함)에 닿기 전까지만 올라가 날짜 표기를 찾고, 찾으면 요약문 앞에 붙인다.
LINK_CONTEXT_JS = r"""
(a) => {
  const block = a.closest('li, article, section, div');
  let ctx = ((block ? block.innerText : a.innerText) || '').trim();
  const DATES = /(\d{1,3}\s*(분|시간|일|주)\s*전(?![가-힣])|어제|20\d{2}\.\s*\d{1,2}\.\s*\d{1,2}\.)/g;
  const DATE = /(\d{1,3}\s*(분|시간|일|주)\s*전(?![가-힣])|어제|20\d{2}\.\s*\d{1,2}\.\s*\d{1,2}\.)/;
  if (!DATE.test(ctx.slice(0, 400))) {
    let el = block ? block.parentElement : null;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
      if (el.tagName === 'UL' || el.tagName === 'OL' || el.tagName === 'BODY') break;
      const t = (el.innerText || '');
      if (t.length > 1500) break;
      // 날짜 표기가 둘 이상이면 이웃 결과까지 담은 칸이다: 남의 날짜를 빌리지 않고 멈춘다(S16 리뷰)
      const all = t.match(DATES) || [];
      if (all.length > 1) break;
      const m = t.match(DATE);
      if (m) { ctx = m[0] + ' · ' + ctx; break; }
    }
  }
  return ctx.slice(0, 400);
}
"""
