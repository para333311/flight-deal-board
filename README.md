# flight-deal-board (제제보드)

항공권 특가/프로모션 게시판을 여러 소스에서 수집해 텔레그램으로 알리는 Flask 앱.
`main` 브랜치가 Render에 배포된다.

## 동작 방식

1. **수집** (`check_airline_deals`, 기본 30분마다) — `config.json`의
   `deal_boards`에 등록된 소스를 병렬로 훑어 새 글을 찾는다.
2. **대기** (`pending_deals`) — 새 글은 텔레그램으로 바로 보내지 않고
   대기 목록에 모아둔다.
3. **정기 발송** (`flush_deal_digest`, 기본 09/12/15/18/21시 KST) — 그 시각까지
   모인 특가를 한 번에 묶어 텔레그램으로 보낸다.

새 글 판정은 링크(정규화된 URL) 기준 dedupe로 이뤄지며, 이미 보낸 글은
`sent_deals` 테이블(또는 `DATABASE_URL`이 없을 때는 `sent_deals.json`
파일)에 기록되어 다시 알리지 않는다.

## 소스 설정 (`config.json`)

```json
{
  "deal_exclude_keyword": "부산출발.호텔.패키지.후기...",
  "deal_include_keyword": "항공권.특가.왕복.도쿄.방콕...",
  "deal_boards": [
    {
      "id": "koreanair_event",
      "name": "대한항공",
      "type": "html",
      "url": "https://www.koreanair.com/kr/ko/promotion/list",
      "enabled": true,
      "priority": 8,
      "keyword": "",
      "exclude_keyword": ""
    }
  ]
}
```

각 소스(`deal_boards` 항목) 필드:

| 필드 | 설명 |
|---|---|
| `id` | 소스 식별자 (영문, 로그/디버그용) |
| `name` | 텔레그램 메시지의 "출처"로 표시되는 이름 |
| `type` | `rss` 또는 생략(=html, 범용 스크레이퍼) |
| `url` | 수집 대상 URL |
| `enabled` | `false`면 이 소스는 완전히 건너뛴다 (네트워크 요청 자체를 하지 않음) |
| `priority` | 점수 가산점 베이스. 공식 항공사 이벤트 페이지처럼 신뢰도 높은 소스는 높게 (예: 8), 일반 커뮤니티 핫딜은 낮게 (예: 5) |
| `keyword` | 포함 키워드 (마침표로 구분, OR 매칭). 비어 있으면 전역 `deal_include_keyword`를 대신 쓴다 |
| `exclude_keyword` | 제외 키워드. 비어 있으면 전역 `deal_exclude_keyword`를 대신 쓴다 |

> **주의**: `keyword`/`exclude_keyword`는 `split_keywords()`가 마침표(`.`)를
> OR 구분자로 쓴다. 그래서 도메인처럼 마침표가 들어간 문자열
> (`v.daum.net` 등)을 그대로 넣으면 `v`/`daum`/`net`으로 쪼개져 버리고,
> 그중 `net`이나 `com` 같은 흔한 조각이 무관한 글까지 다 걸러내는
> 사고로 이어진다 (실제로 겪음). 도메인/URL 조각을 걸러야 할 때는
> 마침표 없는 대표 단어(예: `daum`)만 쓸 것.

**소스 켜고 끄기**: 지금 실제로 켜져(`enabled: true`) 있고 배포 후
`/api/deals/debug`로 정상 수집 확인된 소스는 뽐뿌/뽐뿌RSS/해외뽐뿌/
클리앙알뜰구매/루리웹핫딜(기존 커뮤니티 5곳)과 Google News RSS다.

국내 항공사 공식 이벤트 페이지 9곳(대한항공/아시아나/제주항공/진에어/
티웨이/에어부산/에어서울/에어프레미아/에어로케이)과 알구몬 검색,
그리고 추측으로 넣었던 RSS 대체 경로 3곳은 실제 배포 확인 결과
403(봇 차단)/404/타임아웃/빈 SPA 응답으로 전부 막혀 있어 `enabled: false`로
꺼놨다. 항공사 사이트들은 대부분 Cloudflare 등으로 스크래핑을 막아둔
것으로 보여, 단순 재시도로는 해결되지 않을 가능성이 높다 (URL이 바뀐
경우도 있음 — 에어프레미아는 404).

루리웹 외 일반 커뮤니티(에펨코리아/더쿠), 해외 특가 블로그(SecretFlying
등), 그 외 외항사 프로모션 페이지는 아직 시도해보지 않은 소스라 등록만
해두고 껐다. 켜려면 `config.json`에서 해당 항목의 `"enabled": false`를
`true`로 바꾸고 배포 후 `/api/deals/debug?pw=1111&include_disabled=1`로
실제로 잡히는지 꼭 확인할 것.

### 수집기(fetcher) 종류

- **RSS** (`type: "rss"`) — 표준 RSS 2.0 파싱. 가장 안정적이라 우선한다.
- **HTML** (그 외) — `scrape_board()`가 3단계로 시도한다:
  1. 알려진 게시판 목록/테이블 CSS 선택자
  2. 그래도 안 잡히면 `.title`, `.subject` 등 범용 클래스
  3. 그래도 안 잡히면(공식 이벤트 페이지처럼 구조를 모르는 경우) 페이지의
     모든 `<a>` 태그 텍스트를 키워드로 직접 매칭하는 최후 수단

네이버/구글 검색 결과 HTML이나 항공사 SPA(JS 렌더링) 페이지처럼 구조가
불안정한 곳은 애초에 정식 소스로 넣지 않았다 (Google News는 HTML 검색이
아니라 안정적인 RSS 엔드포인트를 쓴다). 다만 국내 항공사 공식 이벤트
페이지 중 상당수가 JS로 렌더링될 수 있어, 배포 후 `/api/deals/debug`로
실제로 글이 잡히는지 확인이 필요하다 (아래 참고).

## 안정성 장치

- **소스 하나 실패해도 전체가 죽지 않는다.** 각 소스는 개별 스레드에서
  수집되며(`DEAL_FETCH_CONCURRENCY`, 기본 5개 동시), 타임아웃/에러는
  로그만 남기고 다른 소스 수집을 막지 않는다.
- **중복 방지.** `canonicalize_url()`이 `utm_*`, `fbclid`, `gclid` 등
  추적 파라미터를 제거하고 URL을 정규화해 dedupe 키로 쓴다. 같은 글이
  공유 링크 파라미터만 다르게 여러 소스에 실려도 한 번만 알린다.
- **회차당 알림 상한.** `DEAL_MAX_ALERTS_PER_RUN`(기본 20)을 넘는 새 글이
  한 회차에 몰리면, 우선순위(소스 `priority` + 제목의 특가성 단어 가산점)가
  높은 것부터 상한만큼만 이번 회차에 확정하고 나머지는 **버리지 않고
  다음 회차로 이월**한다 (사라지는 게 아니라 다음 수집 때 다시 후보로
  검토된다).
- **오래된 dedupe 기록 정리.** DB 사용 시 `sent_deals`에서 45일 지난
  기록은 자동으로 지운다 (dedupe 목적으로는 충분한 보관 기간).
- **오래된 뉴스 제외.** Google News RSS처럼 재색인으로 몇 달 전 기사가
  최신 글처럼 다시 노출되는 경우, 작성일이 `DEAL_MAX_AGE_DAYS`(기본
  14일)보다 오래됐으면 알림 후보에서 뺀다. 작성일을 아예 못 구한 글
  (공식 이벤트 페이지 등)은 걸러낼 근거가 없으니 그대로 통과시킨다.
- **Google News 링크 단순화.** Google News RSS의 `<link>`는 원문과 무관한
  긴 리다이렉트 토큰 URL이다. `simplify_google_news_link()`가 먼저
  `googlenewsdecoder` 패키지로 디코딩을 시도하고, 실패하면 실제 서버
  리다이렉트를 따라가는 방식으로 폴백한다 (그마저 실패하면 원래 링크
  그대로). GoogleNews 소스로 수집한 글은 스크랩 시점(`scrape_rss`)과
  대기 목록 저장 시점(`add_pending_deals`), 전송 직전(`flush_deal_digest`)
  세 군데에서 한 번씩 더 시도해 웬만하면 짧은 원문 링크로 나가게 한다.
  성공 여부와 무관하게 가독성 자체는 메시지 포맷에서 보장한다 — 아래 참고.
- **Google News 검색은 "최신 글 목록"이 아니라 "지금 시점 상위 결과"
  스냅샷이다.** `q=` 검색어 자체는 결과가 며칠씩 거의 안 바뀔 수 있어,
  구글이 상위로 계속 올려두는 같은 기사들만 반복해서 잡히고 정작 새
  글은 안 나오는 정체 현상이 생길 수 있다. URL에 `when:3d` 연산자를
  붙여 검색 자체를 최근 3일로 제한해뒀다 — 매번 같은 스냅샷이 아니라
  실제로 최근에 나온 기사 위주로 결과가 자연스럽게 갱신된다.

## 텔레그램 메시지 형식

메시지는 `parse_mode: HTML`로 보낸다. 링크는 URL을 그대로 노출하지 않고
"링크"라는 짧은 글자에 매달아, Google News처럼 URL 자체가 아주 길어도
메시지가 항상 짧고 읽기 쉽다. 제목은 외부에서 긁어온 텍스트이므로
`html.escape()`로 이스케이프해 `<`, `>`, `&` 등이 섞여 있어도 메시지가
깨지지 않는다. 출처/매칭 키워드 줄은 넣지 않는다 (사용자 요청으로 제거).

```
✈️ 항공 특가 모음 3건 (08/05 15:00)

🔥 [대한항공] 도쿄 왕복 특가 오픈
🔗 링크          ← 실제로는 클릭 가능한 링크로 표시됨 (원본 URL은 숨김)
```

## 실행 명령어

```bash
# 의존성 설치
pip install -r requirements.txt

# 테스트
python -m unittest discover -s tests

# 특가 수집 1회 실행 (dry-run) — 텔레그램으로 보내지 않고 콘솔에 메시지 미리보기만 출력
python app.py --deals-once

# 대기 중인 특가를 지금 즉시 정기 알림으로 발송 (TELEGRAM_* 설정 시 실제 전송)
python app.py --deals-flush-once

# 로컬 개발 서버 (Flask, 구청 게시판 크롤링용 레거시 진입점)
python app.py
```

배포 환경에서는 이 CLI를 쓸 필요 없이 gunicorn이 모듈을 임포트하는
시점에 APScheduler 잡(수집 주기 잡 + 정기 발송 잡)이 자동 등록된다.

## 진단용 API (관리자 비밀번호 필요, 기본 `1111`)

- `GET /api/deals/debug?pw=1111` — 소스별 HTTP 상태, 수집 건수, 샘플
  제목을 보여준다. `enabled: false`인 소스는 기본적으로 건너뛰며,
  `?include_disabled=1`을 붙이면 꺼진 소스도 실제로 확인해볼 수 있다.
- `GET /api/deals/check?pw=1111` — 즉시 1회 수집(대기 목록에만 쌓임).
- `GET /api/deals/digest?pw=1111` — 대기 목록을 지금 바로 발송.
- `GET /api/telegram/test?pw=1111` — 텔레그램 연결 테스트 메시지 발송.

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (없음) | 텔레그램 봇 토큰. 없으면 전송은 로그만 남기고 스킵 |
| `TELEGRAM_CHAT_ID` | (없음) | 알림 보낼 채팅/채널 ID |
| `DATABASE_URL` | (없음) | PostgreSQL 연결 문자열. 없으면 파일(`sent_deals.json`, `pending_deals.json`)로 대체 — 재배포 시 초기화되므로 운영에는 DB 권장 |
| `DEAL_CHECK_INTERVAL_MINUTES` | `30` | 소스 수집 주기 (분) |
| `DEAL_MAX_ALERTS_PER_RUN` | `20` | 회차당 새로 대기 목록에 추가할 특가 상한 |
| `DEAL_FETCH_CONCURRENCY` | `5` | 소스 병렬 수집 동시 실행 수 |
| `DEAL_MAX_AGE_DAYS` | `14` | 작성일을 아는 글 중 이보다 오래된 건 알림에서 제외 |
| `DEAL_DIGEST_TIMES` | `09:00,12:00,15:00,18:00,21:00` | 정기 알림 시각(KST, 콤마 구분). 설정하면 이 시각에만 발송 |
| `DEAL_DIGEST_INTERVAL_HOURS` | `3` | `DEAL_DIGEST_TIMES`를 비우면 대신 N시간마다 정각 발송 |

`ADMIN_PASSWORD`(진단/수동 실행 API 비밀번호, 기본 `1111`)는 아직 env가
아니라 코드에 하드코딩돼 있다. 외부에 공개된 URL이라면 바꿔서 쓰는 걸
권장한다.
