# flight-deal-board (제제보드)

두 가지를 하는 Flask 앱. `main` 브랜치가 Render에 배포된다.

1. **주말 항공권 추천** — 네이버 항공권에서 '토~월 2박 3일' 왕복 최저가를
   훑어 텔레그램으로 보낸다. (아래 참고)
2. **도시정비 게시판 대시보드** — 서울시/각 구청 재개발·신속통합기획 관련
   게시판을 수집해 웹으로 보여준다.

> 커뮤니티 핫딜 게시판에서 '항공권 특가' 글을 긁어오던 방식은 실효성이 없어
> 폐기했다 (2026-09-17). 지금은 게시판 글이 아니라 네이버 항공권의 실제
> 운임을 직접 조회한다.

## 주말 항공권 추천 (`flight_search.py`)

월요일 하루만 휴가를 쓰는 일정이라 **출발은 토요일, 귀국은 월요일(2박 3일)**
로 고정한다. 앞으로 3개월 안의 모든 토요일 × 후보 도시(34곳)를 훑어
**40만원 이하**만 남기고, 싼 순으로 정렬해 텔레그램으로 보낸다.

**나라별 쿼터**: 그냥 최저가순으로 두면 노선이 많은 일본·중국이 목록을 통째로
차지한다. 그래서 `나라당 최대 3곳`, `도시당 1개`로 제한한다. 도시 상한은 같은
도시가 날짜만 바꿔 그 나라 쿼터를 다 먹어버리는 것을 막기 위한 것이다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `FLIGHT_ORIGIN` | `ICN` | 출발 공항 |
| `FLIGHT_MAX_PRICE` | `400000` | 가격 상한 (원) |
| `FLIGHT_SEARCH_MONTHS` | `3` | 앞으로 몇 개월치를 볼지 |
| `FLIGHT_PER_COUNTRY` | `3` | 나라당 추천 개수 |
| `FLIGHT_PER_CITY` | `1` | 도시당 추천 개수 |
| `FLIGHT_MAX_RESULTS` | `20` | 메시지에 담을 총 개수 |
| `FLIGHT_DIGEST_TIMES` | `09:00` | 발송 시각 (KST, 콤마 구분) |
| `FLIGHT_FETCH_CONCURRENCY` | `4` | 동시 요청 수 |
| `FLIGHT_FAIL_FAST_AFTER` | `15` | 한 건도 못 가져온 채 이만큼 실패하면 중단 |

### 수집 계층이 깨졌을 때

네이버 내부 API는 공개 스펙이 없고 개발 환경에서는 외부 접근이 막혀 있어,
`FETCHERS`의 요청 레시피는 **실물로 검증되지 않았다**. 그래서:

- `extract_min_fare()`가 응답 스키마를 고정하지 않고 JSON 어디에 있든
  '항공료로 보이는 값'을 찾는다. 응답만 받아오면 구조가 달라도 동작한다.
- 수집이 한 건도 성공하지 못하면 "특가 없음"이 아니라 **"수집 실패"**를
  보낸다. 이 둘을 뭉뚱그리면 네이버가 막혔을 때 거짓 메시지가 나간다.
- 원인은 `GET /api/flights/probe?pw=1111&telegram=1` 로 확인한다. 레시피별
  HTTP 상태·응답 앞부분을 텔레그램으로도 보내준다. 실제 응답을 보고
  `NAVER_INTERNATIONAL_QUERY` 블록만 고치면 된다.

## 동작 방식

1. **수집** (`background_scrape`, 기본 30분마다) — `config.json`의 `boards`에
   등록된 소스를 훑어 새 글을 찾는다.
2. **캐시** — 수집 결과를 `cache.json`(DB 미사용 시)에 저장하고, `/`와
   `/api/scrape_all`이 이 캐시를 반환한다.

## 소스 설정 (`config.json`)

```json
{
  "boards": [
    {
      "name": "서울시결재문서",
      "url": "https://opengov.seoul.go.kr/sanction/list",
      "keyword": "재개발.신속통합.일대.후보지.수권.정비계획.동의서"
    }
  ]
}
```

각 소스(`boards` 항목) 필드:

| 필드 | 설명 |
|---|---|
| `name` | 대시보드에 표시되는 게시판 이름 |
| `url` | 수집 대상 URL |
| `keyword` | 포함 키워드 (마침표로 구분, OR 매칭). 비어 있으면 모든 글을 수집 |
| `exclude_keyword` | 제외 키워드 (마침표로 구분). 생략 가능 |

### 수집기(fetcher) 종류

- **서울 정보소통광장** (`opengov.seoul.go.kr`) — 공식 포털 API
  (`scrape_open_portal`)를 우선 사용하고, 실패하면 네이버 검색 결과를 파싱하는
  폴백(`scrape_opengov_search_fallback`)으로 넘어간다.
- **RSS** (`rss.php`가 URL에 포함된 경우) — 표준 RSS 2.0 파싱.
- **HTML** (그 외) — `scrape_board()`가 3단계로 시도한다:
  1. 알려진 게시판 목록/테이블 CSS 선택자
  2. 그래도 안 잡히면 `.title`, `.subject` 등 범용 클래스
  3. 그래도 안 잡히면 페이지의 모든 `<a>` 태그 텍스트를 키워드로 직접 매칭하는
     최후 수단

## 안정성 장치

- **중복 방지.** `canonicalize_url()`이 `utm_*`, `fbclid`, `gclid` 등 추적
  파라미터를 제거하고 URL을 정규화해 dedupe 키로 쓴다.

## 실행 명령어

```bash
# 의존성 설치
pip install -r requirements.txt

# 테스트
python -m unittest discover -s tests

# 로컬 개발 서버 (Flask)
python app.py
```

배포 환경에서는 gunicorn이 모듈을 임포트하는 시점에 방문자 테이블이
초기화되고, `python app.py`로 직접 실행할 때만 30분 주기 크롤링 스케줄러가
등록된다.

## API

- `GET /` — 대시보드 페이지.
- `GET /api/scrape_all` — 캐시된 수집 결과 반환 (없으면 즉시 크롤링).
- `POST /api/refresh` — 즉시 크롤링 실행.
- `POST /api/boards` / `DELETE /api/boards` — 게시판 소스 추가/삭제 (관리자
  비밀번호 필요, 기본 `1111`).
- `GET /api/visitors` / `POST /api/visitors` — 방문자 수 조회/증가.
- `GET /api/flights/run?pw=1111` — 주말 항공권을 지금 즉시 검색해 텔레그램 발송.
- `GET /api/flights/probe?pw=1111&telegram=1` — 네이버 수집 경로 진단.

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | (없음) | PostgreSQL 연결 문자열. 없으면 파일(`visitors.json`)로 대체 — 재배포 시 초기화되므로 운영에는 DB 권장 |
| `TELEGRAM_BOT_TOKEN` | (없음) | 텔레그램 봇 토큰. 없으면 발송 스킵 + 스케줄러 잡도 등록되지 않음 |
| `TELEGRAM_CHAT_ID` | (없음) | 알림 보낼 채팅/채널 ID |

`ADMIN_PASSWORD`(게시판 관리 API 비밀번호, 기본 `1111`)는 아직 env가 아니라
코드에 하드코딩돼 있다. 외부에 공개된 URL이라면 바꿔서 쓰는 걸 권장한다.
