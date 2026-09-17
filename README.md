# flight-deal-board (제제보드)

서울시/각 구청 도시정비(재개발·신속통합기획 등) 관련 게시판을 여러 소스에서
수집해 대시보드로 보여주는 Flask 앱. `main` 브랜치가 Render에 배포된다.

> 과거에는 항공권 특가 게시판을 텔레그램으로 알리는 기능도 있었지만, 실효성이
> 없어 폐기했다 (2026-09-17).

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

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | (없음) | PostgreSQL 연결 문자열. 없으면 파일(`visitors.json`)로 대체 — 재배포 시 초기화되므로 운영에는 DB 권장 |

`ADMIN_PASSWORD`(게시판 관리 API 비밀번호, 기본 `1111`)는 아직 env가 아니라
코드에 하드코딩돼 있다. 외부에 공개된 URL이라면 바꿔서 쓰는 걸 권장한다.
