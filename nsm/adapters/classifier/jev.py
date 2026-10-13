"""MotherDuck `prompt_jev()` 분류기 (ADR 0003).

choice 모드 반환값: STRUCT(choice VARCHAR, probabilities ..., confidence DOUBLE).
instructions·choice는 상수여야 해서 SQL 리터럴로 넣고(작은따옴표 이스케이프), 입력 텍스트만 바인딩한다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nsm.adapters.pricing import JEV_PRICE_PER_ROW
from nsm.domain.models import Classification
from nsm.ports import AdapterError, CostSink


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_sql(instruction: str, labels: list[str], from_table: bool) -> str:
    choices = "[" + ", ".join(_lit(lbl) for lbl in labels) + "]"
    arg = "t" if from_table else "?"
    sql = f"SELECT prompt_jev({arg}, {_lit(instruction)}, choice := {choices}) AS r"
    if from_table:
        sql = (
            f"SELECT idx, prompt_jev(t, {_lit(instruction)}, choice := {choices}) AS r "
            "FROM nsm_jev_input ORDER BY idx"
        )
    return sql


def _to_classification(r: Any, labels: list[str]) -> Classification:
    if not isinstance(r, dict):
        raise AdapterError(f"prompt_jev 응답 형식이 예상과 다릅니다: {r!r}")
    label = r.get("choice")
    if label not in labels:
        raise AdapterError(f"prompt_jev가 허용되지 않은 라벨을 돌려줬습니다: {label!r}")
    return Classification(label=label, confidence=min(1.0, max(0.0, _choice_probability(r))))


def _choice_probability(r: dict[str, Any]) -> float:
    """고른 라벨의 확률. jev의 `confidence`는 확률이 아니라 1등과 2등 확률의 차이다
    (relevant 0.70 / irrelevant 0.30 → confidence 0.40). 다른 분류기와 같은 뜻("이 라벨일 확률")으로
    맞추려고 probabilities에서 고른 라벨의 값을 쓴다. 없으면 두 라벨일 때의 환산 (1 + 차이) / 2.
    """
    for p in r.get("probabilities") or []:
        if (
            isinstance(p, dict)
            and p.get("value") == r.get("choice")
            and p.get("probability") is not None
        ):
            return float(p["probability"])
    return (1.0 + float(r.get("confidence") or 0)) / 2


class JevClassifier:
    name = "jev"

    def __init__(
        self,
        token: str,
        cost_sink: CostSink | None = None,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._token = token
        self.cost_sink = cost_sink
        self._connect = connect
        self._conn: Any = None

    def _db(self) -> Any:
        if self._conn is None:
            if self._connect is not None:
                self._conn = self._connect(self._token)
            else:
                import duckdb

                # 토큰을 연결 문자열에 넣지 않는다(오류 메시지에 경로가 찍힐 수 있음)
                self._conn = duckdb.connect("md:", config={"motherduck_token": self._token})
        return self._conn

    def _scrub(self, e: Exception) -> str:
        return str(e).replace(self._token, "***") if self._token else str(e)

    def _record(self, rows: int) -> None:
        if self.cost_sink:
            self.cost_sink.record_cost(
                "classifier", "motherduck-jev", rows * JEV_PRICE_PER_ROW, "estimate"
            )

    def classify(self, text: str, labels: list[str], instruction: str) -> Classification:
        try:
            row = self._db().execute(build_sql(instruction, labels, False), [text]).fetchone()
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AdapterError(f"prompt_jev 호출 실패: {self._scrub(e)}") from None
        self._record(1)
        return _to_classification(row[0], labels)

    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]:
        if not texts:
            return []
        try:
            db = self._db()
            db.execute("CREATE OR REPLACE TEMP TABLE nsm_jev_input (idx INTEGER, t VARCHAR)")
            db.executemany("INSERT INTO nsm_jev_input VALUES (?, ?)", list(enumerate(texts)))
            rows = db.execute(build_sql(instruction, labels, True)).fetchall()
        except Exception as e:  # noqa: BLE001
            raise AdapterError(f"prompt_jev 일괄 호출 실패: {self._scrub(e)}") from None
        self._record(len(texts))
        return [_to_classification(r[1], labels) for r in rows]
