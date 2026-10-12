# flight-deal-board

Flask 앱. Render에 `main` 브랜치가 배포된다. 두 기능이 한 앱에 들어 있다.

1. **주말 항공권 추천** (`flight_search.py`) — 네이버 항공권에서 토~월 2박3일
   왕복 최저가를 훑어 텔레그램 발송.
2. **도시정비 게시판 대시보드** (`app.py`의 `boards`) — 구청 게시판 수집.

> 커뮤니티 게시판에서 '항공권 특가' 글을 긁어오던 방식(`deal_boards`)은
> 실효성이 없어 폐기했다 (사용자 지시, 2026-09-17). 대신 네이버 항공권의
> 실제 운임을 직접 조회하는 방식으로 바꿨다.

## 작업 방식

- **머지까지 알아서 진행한다.** 작업이 끝나면 확인을 기다리지 말고 브랜치에
  푸시 → PR 생성 → `main`에 머지까지 마친다. (사용자 지시, 2026-07-27)
  `main`에 머지해야 Render에 실제로 배포되므로, 머지하지 않으면 변경이
  동작하지 않는다.
- 머지 전에 `python -m unittest discover -s tests` 를 돌려 통과를 확인한다.
- 커밋 메시지와 PR 본문은 한국어로 쓴다.

## 구조 메모

- `config.json` — 수집 대상 게시판(`boards`) 설정. 항목별 `name`/`url`/
  `keyword`/`exclude_keyword` 필드는 `README.md` 참고.
- `app.py`
  - `background_scrape()` — 30분마다(또는 `/api/refresh` 호출 시) `boards`를
    훑어 `cache.json`에 저장한다. `/`, `/api/scrape_all`이 이 캐시를 쓴다.
  - `drop_excluded_posts()` — 제목에 제외 키워드가 있으면 걸러낸다.
  - 소스 확장 관련 구조는 `README.md`에 정리해뒀다. 소스를 추가/수정할 때는
    README를 먼저 참고할 것.
- `flight_search.py` — 항공권 검색. 앱 설정에 의존하지 않게 짜여 있어
  (`run_weekend_flight_digest`가 발송 함수를 인자로 받는다) 테스트가 쉽다.
  - **개발 환경에서는 네이버로 나갈 수 없다** (egress 차단). 실물 응답은
    배포 후 Render 로그(기동 진단)나 `/api/flights/probe`,
    `/api/flights/schema`로만 확인할 수 있다. Render MCP의 `list_logs`로
    로그를 직접 읽을 수 있으니, 사람에게 결과를 받아오게 하지 말고
    로그 기반으로 진단할 것.
  - **엔드포인트**: `https://flight-api.naver.com/graphql` (2026-10-12 확정).
    다른 naver.com 호스트는 404/503이거나 전혀 다른 서비스다 — 추측으로
    바꾸지 말 것.
  - **통하는 변수 조합** (`_min_prices_by_date_variables`): `tripType: 'RT'`
    + `groupByDepartureDate: False`. 자세한 근거는 `README.md`.
    - `isNonstop`은 넣는 순간 결과가 통째로 빈다 — 절대 넣지 말 것.
    - `tripDays`는 무시된다. 2박 3일은 `pick_return_row()`가 복귀일 +2일
      행을 골라서 맞춘다.
    - `groupByDepartureDate: True`로 바꾸면 일정 길이를 못 고른다. 그래서
      `collect_offers`는 (도시 × 토요일)마다 한 번씩 요청한다.
  - 이 쿼리는 **시각을 주지 않는다** — `depart_hour`/`return_hour`는 항상
    `None`이고 '오전 출발/오후 복귀' 조건은 아직 검증 못 한다. 시간 정보가
    없다고 후보를 버리면 안 되고(추천이 통째로 빔), 메시지에 '시간
    미확인'으로만 표시한다.
  - 수집 실패(캘리브레이션 실패 포함)와 '조건에 맞는 특가 없음'을 반드시
    구분할 것. 뭉뚱그리면 네이버가 막혔을 때 "특가 없음"이라는 거짓
    알림이 나간다.
