# 0006. 엔진·스케줄러·대시보드를 한 프로세스에 두고, DB는 DuckDB, 대시보드는 FastAPI로 만든다

- 상태: 채택 (Streamlit 계획을 대체) · 날짜: 2026-10-11 · 결정: 선웅제 · 관련: [mvp-review](../mvp-review.md) #1, Round 4 Q24

## 배경

원래 계획은 엔진 프로세스 + 별도 Streamlit 대시보드 프로세스가 같은 DuckDB 파일을 쓰는 것이었다. DuckDB는 한 프로세스가 읽기/쓰기로 열면 다른 프로세스는 읽기조차 못 한다([공식 문서](https://duckdb.org/docs/current/connect/concurrency.html)). 또 메인이 마지막에 UI/UX를 직접 디자인할 예정인데 Streamlit은 화면 커스터마이즈 자유도가 낮다.

용어: DuckDB는 파일 하나로 동작하는 무료 오픈소스 DB(서버 설치 없음, 분석 쿼리에 빠름). FastAPI는 Python 웹 프레임워크.

## 선택지

1. **단일 프로세스 + FastAPI(Jinja2 + HTMX) + DuckDB**
2. **Streamlit 유지 + DB를 SQLite(WAL 모드)로 교체**: 다중 프로세스 읽기 가능 / 디자인 자유도 낮음
3. **Streamlit 유지 + 엔진이 로컬 HTTP API 제공**: 구현량 최대

## 결정

1번. DB 충돌이 원천적으로 없고, 대시보드가 일반 HTML/CSS라 디자인이 자유롭다.

## 결과

- `nsm run` 하나가 APScheduler 스레드와 uvicorn 스레드를 띄우고 DuckDB 연결 하나를 공유한다(스레드별 `cursor()`).
- 대시보드만 따로 띄울 수 없다. 프로세스가 죽으면 대시보드도 죽는다(자동 시작으로 보완).
- 테스트는 `:memory:` DuckDB + FastAPI TestClient로 한 프로세스 안에서 돈다.
