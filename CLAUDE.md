# flight-deal-board

서울시/구청 도시정비(재개발·신속통합기획 등) 게시판을 수집해 대시보드로
보여주는 Flask 앱. Render에 `main` 브랜치가 배포된다.

> 항공권 특가를 텔레그램으로 알리던 기능은 실효성이 없어 폐기했다
> (사용자 지시, 2026-09-17). `deal_boards`/`pending_deals`/텔레그램 발송 관련
> 코드는 모두 제거됐고, `config.json`의 `boards`(구청 게시판 수집)만 남아
> 있다.

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
