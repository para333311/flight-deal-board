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

**소스 켜고 끄기**: 지금은 국내 항공사 공식 이벤트 페이지, 뽐뿌/클리앙류
커뮤니티, Google News RSS, 알구몬 검색까지 `enabled: true`로 켜져 있다.
루리웹/에펨코리아/더쿠 같은 일반 커뮤니티, 해외 특가 블로그(SecretFlying
등), 외항사 프로모션 페이지는 `enabled: false`로 등록만 해뒀다. 켜려면
`config.json`에서 해당 항목의 `"enabled": false`를 `true`로 바꾸면 된다.

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

## 텔레그램 메시지 형식

```
✈️ 항공 특가 모음 3건 (08/05 15:00)

🔥 [대한항공] 도쿄 왕복 특가 오픈
출처: 대한항공
매칭: 왕복, 특가, 오픈특가
링크: https://www.koreanair.com/kr/ko/promotion/...
```

일반 텍스트로 전송하며(parse_mode 미사용) 마크다운/HTML 파싱을 쓰지
않으므로 제목에 특수문자가 있어도 메시지가 깨지지 않는다.

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
| `DEAL_DIGEST_TIMES` | `09:00,12:00,15:00,18:00,21:00` | 정기 알림 시각(KST, 콤마 구분). 설정하면 이 시각에만 발송 |
| `DEAL_DIGEST_INTERVAL_HOURS` | `3` | `DEAL_DIGEST_TIMES`를 비우면 대신 N시간마다 정각 발송 |

`ADMIN_PASSWORD`(진단/수동 실행 API 비밀번호, 기본 `1111`)는 아직 env가
아니라 코드에 하드코딩돼 있다. 외부에 공개된 URL이라면 바꿔서 쓰는 걸
권장한다.
