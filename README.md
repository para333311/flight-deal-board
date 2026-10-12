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

**시간대**: 2박 3일을 온전히 쓰려면 **갈 때는 오전, 올 때는 오후** 비행기여야
한다(`FLIGHT_OUTBOUND_LATEST_HOUR` / `FLIGHT_INBOUND_EARLIEST_HOUR`). 조건을
어기는 게 확인된 편은 뺀다. 다만 시각을 못 얻은 편은 버리지 않고 '시간 미확인'
으로 표시한다 — 시간 정보가 없다는 이유로 전부 날리면 추천이 통째로 비기 때문.

**나라별 쿼터**: 그냥 최저가순으로 두면 노선이 많은 일본·중국이 목록을 통째로
차지한다. 그래서 `나라당 최대 3곳`, `도시당 1개`로 제한한다. 도시 상한은 같은
도시가 날짜만 바꿔 그 나라 쿼터를 다 먹어버리는 것을 막기 위한 것이다.

**호출 방식**: 네이버 항공권의 날짜별 캘린더 위젯이 쓰는 `minPricesByDate`
쿼리를 `https://flight-api.naver.com/graphql`로 직접 부른다. 한 번의 호출은
**그 출발일의 복귀일별 최저가**를 돌려주므로, (도시 × 토요일)마다 한 번씩
호출하고 복귀일이 +2일인 행만 고른다. 34개 도시 × 13주 = 약 442번.

**알려진 한계**: `minPricesByDate`는 가격·날짜만 주고 **시각(오전/오후)은
주지 않는다**. 그래서 "갈 때 오전/올 때 오후" 조건은 아직 서버가 검증해주지
못하고, 메시지에는 항상 '시간 미확인'으로 뜬다. 실제 편별 출발·도착 시각을
주는 쿼리는 아직 못 찾았다 — 검색 결과 페이지가 그리는 상세 목록은 초기
HTML에 링크된 스크립트가 아니라 검색을 실행할 때 동적으로 불러오는 별도
청크에 있는 것으로 보인다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `FLIGHT_ORIGIN` | `ICN` | 출발 공항 |
| `FLIGHT_MAX_PRICE` | `400000` | 가격 상한 (원) |
| `FLIGHT_SEARCH_MONTHS` | `3` | 앞으로 몇 개월치를 볼지 |
| `FLIGHT_PER_COUNTRY` | `3` | 나라당 추천 개수 |
| `FLIGHT_PER_CITY` | `1` | 도시당 추천 개수 |
| `FLIGHT_MAX_RESULTS` | `20` | 메시지에 담을 총 개수 |
| `FLIGHT_DIGEST_ENABLED` | `false` | 항공권 추천 발송 켜기. 2026-09-26 네이버 수집 0건이 사흘 이어져 **꺼 둠** — 다시 할 때 `true` |
| `FLIGHT_DIGEST_TIMES` | `09:00` | 발송 시각 (KST, 콤마 구분) |
| `FLIGHT_FETCH_CONCURRENCY` | `4` | 동시 요청 수 |
| `FLIGHT_FAIL_FAST_AFTER` | `15` | 한 건도 못 가져온 채 이만큼 실패하면 중단 |
| `FLIGHT_OUTBOUND_LATEST_HOUR` | `12` | 출발편은 이 시각 전에 출발 (오전) |
| `FLIGHT_INBOUND_EARLIEST_HOUR` | `12` | 복귀편은 이 시각 이후 출발 (오후) |

### 실제 쿼리문을 알아낸 과정 (참고용)

개발 환경에서는 네이버로 나갈 수 없어 아래 순서로 배포 후 진단했다.

1. **introspection** — 네이버가 운영 환경에서 꺼놨다(`__schema` 요청 거부).
   그래서 조회 코드는 지웠다 — 쓸 수 없는 길이다.
2. **페이지 내장 JSON** — 없음. HTML은 껍데기만 오고 운임은 XHR로 불러온다.
   (`inspect_page`도 같은 이유로 지웠다)
3. **JS 번들 스캔** (`discover_queries`) — 성공. introspection이 막혀 있어도
   브라우저가 보내는 쿼리문 자체는 번들에 문자열로 그대로 실려 있다.
   `<script src>`를 받아 실제 쿼리 정의를 추출해 `minPricesByDate`를
   찾아냈다. `GET /api/flights/schema?pw=1111&telegram=1`로 다시 확인할 수
   있다.
4. **엔드포인트 확정** — 쿼리문은 맞는데 계속 "모르는 필드"로 거부당했다.
   후보 호스트를 전부 던져본 결과 `flight-api.naver.com/graphql`만 200 +
   실제 행을 돌려줬다(나머지는 404/503/HTML). 처음 찍어둔 주소는 다른
   서비스라, 그럴듯하지만 엉뚱한 에러를 열 번쯤 돌려줬다.
5. **변수 조합 분리 실험** — 인자를 하나씩만 바꿔 배포하고 Render 로그를
   읽어 `isNonstop`이 결과를 비우는 원인임을 확정했다. 사람이 진단 URL을
   눌러 결과를 복사해 주는 왕복을 없앤 게 결정적이었다.

### 통하는 변수 조합 (2026-10-12 확정)

`minPricesByDate`의 인자는 (enum이 아니라) 문자열·불리언이라 값이 틀려도
GraphQL이 에러를 주지 않는다 — 그냥 **빈 결과로 조용히** 돌아온다. 실제로
행이 오는 조합은 배포 후 로그 진단으로 하나씩 분리해 확정했다.

```json
{
  "departureLocationCode": "ICN",
  "arrivalLocationCode": "FUK",
  "departureDate": "20261017",
  "tripType": "RT",
  "groupByDepartureDate": false
}
```

- `groupByDepartureDate: false` 가 핵심이다. `true`면 그 출발일의 '아무
  복귀일이나 포함한 최저가' 한 줄만 와서(복귀일이 25일 뒤로 오기도 한다)
  2박 3일을 고를 수 없다. `false`면 복귀일별로 한 줄씩 오므로
  `pick_return_row()`가 +2일 행을 집어낸다.
- `isNonstop`은 **넣으면 안 된다** — 값과 무관하게 결과가 통째로 빈다.
- `tripDays`는 무시된다. 일정 길이는 복귀일을 직접 골라 맞춘다.
- `departureLocationType`/`arrivalLocationType`은 넣어도 되지만(`AIRPORT`
  확인) 필요 없어서 빼뒀다.

실제로 확인한 응답 한 줄 (ICN→후쿠오카, 토 출발 월 귀국):

```json
{"departureDate": "20261017", "returnDate": "20261019", "minPrice": 345500}
```

- 수집이 한 건도 성공하지 못하면 "특가 없음"이 아니라 **"수집 실패"**를
  보낸다. 이 둘을 뭉뚱그리면 네이버가 막혔을 때 거짓 메시지가 나간다.
- 수집이 멈추면 `GET /api/flights/probe?pw=1111&telegram=1` 부터 본다.
  어느 단계에서 끊겼는지(쿠키 / HTTP 상태 / 복귀일 행 유무 / 2박3일 행
  유무)와 표본 추천까지 한 번에 보여준다.
- 네이버가 쿼리문 자체를 바꿔 깨졌다면
  `GET /api/flights/schema?pw=1111&op=minPricesByDate` 로 번들에서 현재
  쿼리문을 다시 긁어온다.

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
- `GET /api/flights/probe?pw=1111&telegram=1` — 수집 경로 진단 (레시피가 지금도 통하는지).
- `GET /api/flights/schema?pw=1111&op=minPricesByDate` — JS 번들에서 현재 쿼리문 재확보.

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | (없음) | PostgreSQL 연결 문자열. 없으면 파일(`visitors.json`)로 대체 — 재배포 시 초기화되므로 운영에는 DB 권장 |
| `TELEGRAM_BOT_TOKEN` | (없음) | 텔레그램 봇 토큰. 없으면 발송 스킵 + 스케줄러 잡도 등록되지 않음 |
| `TELEGRAM_CHAT_ID` | (없음) | 알림 보낼 채팅/채널 ID |

`ADMIN_PASSWORD`(게시판 관리 API 비밀번호, 기본 `1111`)는 아직 env가 아니라
코드에 하드코딩돼 있다. 외부에 공개된 URL이라면 바꿔서 쓰는 걸 권장한다.
