"""네이버 항공권 기반 '토~월 2박 3일' 주말 특가 검색.

월요일 하루만 휴가를 쓰는 일정이라 출발은 토요일, 귀국은 월요일로 고정한다.
앞으로 3개월 안의 모든 토요일 × 후보 도시를 훑어 가격 상한 이하만 남기고,
나라별 쿼터를 적용해 추천 목록을 만든 뒤 텔레그램으로 보낸다.

네트워크 계층(`FETCHERS`)은 네이버 내부 API를 직접 부르는 부분이라 개발
환경에서는 검증할 수 없다(외부 접근이 막혀 있음). 그래서 응답 스키마를
고정하지 않고 `extract_min_fare()`가 JSON 어디에 있든 항공료로 보이는 값을
찾아내며, 어떤 레시피가 실제로 통했는지는 `probe()`로 확인한다.
"""

import calendar
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

ORIGIN = os.environ.get('FLIGHT_ORIGIN', 'ICN')
MAX_PRICE = int(os.environ.get('FLIGHT_MAX_PRICE', '400000'))
SEARCH_MONTHS = int(os.environ.get('FLIGHT_SEARCH_MONTHS', '3'))
PER_COUNTRY = int(os.environ.get('FLIGHT_PER_COUNTRY', '3'))
PER_CITY = int(os.environ.get('FLIGHT_PER_CITY', '1'))
MAX_RESULTS = int(os.environ.get('FLIGHT_MAX_RESULTS', '20'))
FETCH_CONCURRENCY = int(os.environ.get('FLIGHT_FETCH_CONCURRENCY', '4'))
FETCH_TIMEOUT = int(os.environ.get('FLIGHT_FETCH_TIMEOUT', '20'))
# 한 건도 못 가져온 채 이만큼 실패하면 수집을 접는다 (네이버가 막힌 경우)
FAIL_FAST_AFTER = int(os.environ.get('FLIGHT_FAIL_FAST_AFTER', '15'))
TRIP_NIGHTS = 2  # 토요일 출발 → 월요일 귀국

WEEKDAY_KO = ('월', '화', '수', '목', '금', '토', '일')
SATURDAY = 5
ORIGIN_NAMES = {'ICN': '인천', 'GMP': '김포', 'PUS': '부산', 'CJU': '제주'}

# 2박 3일로 다녀올 수 있고 40만원대 왕복이 실제로 나오는 아시아권만 둔다.
# (유럽·미주는 이 일정으로는 성립하지 않아 요청 수만 늘린다)
DESTINATIONS = (
    {'code': 'NRT', 'city': '도쿄', 'country': '일본'},
    {'code': 'KIX', 'city': '오사카', 'country': '일본'},
    {'code': 'FUK', 'city': '후쿠오카', 'country': '일본'},
    {'code': 'CTS', 'city': '삿포로', 'country': '일본'},
    {'code': 'OKA', 'city': '오키나와', 'country': '일본'},
    {'code': 'NGO', 'city': '나고야', 'country': '일본'},
    {'code': 'PVG', 'city': '상하이', 'country': '중국'},
    {'code': 'PEK', 'city': '베이징', 'country': '중국'},
    {'code': 'TAO', 'city': '칭다오', 'country': '중국'},
    {'code': 'HGH', 'city': '항저우', 'country': '중국'},
    {'code': 'TPE', 'city': '타이베이', 'country': '대만'},
    {'code': 'KHH', 'city': '가오슝', 'country': '대만'},
    {'code': 'HKG', 'city': '홍콩', 'country': '홍콩'},
    {'code': 'MFM', 'city': '마카오', 'country': '마카오'},
    {'code': 'DAD', 'city': '다낭', 'country': '베트남'},
    {'code': 'SGN', 'city': '호치민', 'country': '베트남'},
    {'code': 'HAN', 'city': '하노이', 'country': '베트남'},
    {'code': 'CXR', 'city': '나트랑', 'country': '베트남'},
    {'code': 'PQC', 'city': '푸꾸옥', 'country': '베트남'},
    {'code': 'BKK', 'city': '방콕', 'country': '태국'},
    {'code': 'CNX', 'city': '치앙마이', 'country': '태국'},
    {'code': 'HKT', 'city': '푸껫', 'country': '태국'},
    {'code': 'CEB', 'city': '세부', 'country': '필리핀'},
    {'code': 'MNL', 'city': '마닐라', 'country': '필리핀'},
    {'code': 'KLO', 'city': '보라카이', 'country': '필리핀'},
    {'code': 'SIN', 'city': '싱가포르', 'country': '싱가포르'},
    {'code': 'KUL', 'city': '쿠알라룸푸르', 'country': '말레이시아'},
    {'code': 'BKI', 'city': '코타키나발루', 'country': '말레이시아'},
    {'code': 'DPS', 'city': '발리', 'country': '인도네시아'},
    {'code': 'GUM', 'city': '괌', 'country': '괌'},
    {'code': 'SPN', 'city': '사이판', 'country': '사이판'},
    {'code': 'UBN', 'city': '울란바토르', 'country': '몽골'},
    {'code': 'REP', 'city': '시엠립', 'country': '캄보디아'},
    {'code': 'VTE', 'city': '비엔티안', 'country': '라오스'},
)


def add_months(base, months):
    """달력 기준으로 N개월 뒤 날짜. (말일이 없는 달은 그 달의 말일로 맞춘다)"""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def iter_weekend_trips(today=None, months=SEARCH_MONTHS, nights=TRIP_NIGHTS):
    """앞으로 months개월 안의 (토요일 출발, 월요일 귀국) 일정을 모두 만든다."""
    today = today or date.today()
    last_day = add_months(today, months)

    days_ahead = (SATURDAY - today.weekday()) % 7
    if days_ahead == 0:
        # 오늘이 토요일이면 당일 출발은 의미가 없으니 다음 주 토요일부터 본다
        days_ahead = 7

    trips = []
    departure = today + timedelta(days=days_ahead)
    while departure <= last_day:
        trips.append((departure, departure + timedelta(days=nights)))
        departure += timedelta(days=7)
    return trips


def naver_flight_url(origin, destination, departure, return_date):
    """사람이 눌러서 바로 확인/예약할 수 있는 네이버 항공권 검색 URL."""
    return (
        'https://flight.naver.com/flights/international/'
        f'{origin}-{destination}-{departure:%Y%m%d}/'
        f'{destination}-{origin}-{return_date:%Y%m%d}?adult=1&fareType=Y'
    )


# --- 가격 추출 -------------------------------------------------------------

FARE_KEY_HINTS = ('fare', 'price', 'amount', 'total', 'adult', 'charge')
# 원화 왕복 항공권으로 말이 되는 범위. 좌석 수·소요시간·타임스탬프 같은
# 무관한 숫자가 가격으로 잡히는 것을 막는 유일한 안전장치라 좁게 잡는다.
MIN_PLAUSIBLE_FARE = 30000
MAX_PLAUSIBLE_FARE = 5000000


def extract_min_fare(payload):
    """응답 JSON 어디에 있든 '항공료로 보이는 값' 중 최솟값을 찾는다.

    네이버 내부 API의 정확한 스키마에 코드를 묶어두지 않기 위한 장치다.
    키 이름에 fare/price 등이 들어간 숫자만 후보로 보고, 원화 항공권으로
    말이 되는 범위 밖은 버린다.
    """
    best = None
    stack = [(None, payload)]
    while stack:
        key, node = stack.pop()
        if isinstance(node, dict):
            for child_key, value in node.items():
                stack.append((child_key, value))
            continue
        if isinstance(node, list):
            for value in node:
                stack.append((key, value))
            continue
        if not key or not any(hint in key.lower() for hint in FARE_KEY_HINTS):
            continue

        if isinstance(node, bool):
            continue
        if isinstance(node, (int, float)):
            value = int(node)
        elif isinstance(node, str):
            cleaned = node.replace(',', '').strip()
            if not cleaned.isdigit():
                continue
            value = int(cleaned)
        else:
            continue

        if MIN_PLAUSIBLE_FARE <= value <= MAX_PLAUSIBLE_FARE:
            if best is None or value < best:
                best = value
    return best


# --- 네트워크 계층 ---------------------------------------------------------

NAVER_GRAPHQL_URL = 'https://airline-api.naver.com/graphql'
NAVER_PAGE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://flight.naver.com/',
    'Origin': 'https://flight.naver.com',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

# 네이버 SPA가 실제로 쓰는 GraphQL 오퍼레이션 이름과 변수 구조를 따른 것이지만,
# 개발 환경에서 외부 접근이 막혀 있어 실물로 검증하지 못했다. 응답이 비면
# /api/flights/probe 로 실제 응답을 확인해 이 블록만 고치면 된다.
NAVER_INTERNATIONAL_QUERY = """
query getInternationalList($trip: String, $itinerary: [ItineraryInput], $adult: Int, $child: Int, $infant: Int, $fareType: String) {
  internationalList(trip: $trip, itinerary: $itinerary, adult: $adult, child: $child, infant: $infant, fareType: $fareType) {
    isComplete
    results {
      fares {
        fare {
          adultFare
          totalFare
        }
      }
    }
  }
}
"""

EMBEDDED_JSON_RE = re.compile(
    r'(?:__NEXT_DATA__|__APOLLO_STATE__|__PRELOADED_STATE__)\s*=\s*({.*?})\s*[;<]',
    re.DOTALL,
)


def _fetch_via_graphql(origin, destination, departure, return_date, timeout):
    """네이버 항공권 SPA가 쓰는 GraphQL 엔드포인트를 직접 호출한다."""
    payload = {
        'operationName': 'getInternationalList',
        'variables': {
            'trip': 'RT',
            'itinerary': [
                {
                    'departureAirport': origin,
                    'arrivalAirport': destination,
                    'departureDate': f'{departure:%Y%m%d}',
                },
                {
                    'departureAirport': destination,
                    'arrivalAirport': origin,
                    'departureDate': f'{return_date:%Y%m%d}',
                },
            ],
            'adult': 1,
            'child': 0,
            'infant': 0,
            'fareType': 'Y',
        },
        'query': NAVER_INTERNATIONAL_QUERY,
    }
    response = requests.post(
        NAVER_GRAPHQL_URL, json=payload, headers=NAVER_PAGE_HEADERS, timeout=timeout,
    )
    diagnostic = {'status': response.status_code, 'bytes': len(response.content)}
    try:
        body = response.json()
    except ValueError:
        diagnostic['error'] = 'JSON 아님'
        diagnostic['snippet'] = response.text[:300]
        return None, diagnostic
    if isinstance(body, dict) and body.get('errors'):
        diagnostic['error'] = 'GraphQL 오류'
        diagnostic['snippet'] = json.dumps(body['errors'], ensure_ascii=False)[:300]
        return None, diagnostic
    fare = extract_min_fare(body)
    if fare is None:
        diagnostic['error'] = '가격 없음'
        diagnostic['snippet'] = json.dumps(body, ensure_ascii=False)[:300]
    return fare, diagnostic


def _fetch_via_page(origin, destination, departure, return_date, timeout):
    """검색 페이지 HTML에 끼워진 JSON에서 가격을 찾는 폴백 경로."""
    url = naver_flight_url(origin, destination, departure, return_date)
    response = requests.get(url, headers=NAVER_PAGE_HEADERS, timeout=timeout)
    diagnostic = {'status': response.status_code, 'bytes': len(response.content)}
    match = EMBEDDED_JSON_RE.search(response.text)
    if not match:
        diagnostic['error'] = '내장 JSON 없음'
        return None, diagnostic
    try:
        body = json.loads(match.group(1))
    except ValueError:
        diagnostic['error'] = '내장 JSON 파싱 실패'
        return None, diagnostic
    fare = extract_min_fare(body)
    if fare is None:
        diagnostic['error'] = '가격 없음'
    return fare, diagnostic


FETCHERS = (
    ('graphql', _fetch_via_graphql),
    ('page', _fetch_via_page),
)


def search_fare(origin, destination, departure, return_date, timeout=FETCH_TIMEOUT):
    """한 구간의 최저가를 찾는다. 성공한 레시피가 없으면 None."""
    for name, fetcher in FETCHERS:
        try:
            fare, _ = fetcher(origin, destination, departure, return_date, timeout)
        except requests.RequestException:
            continue
        if fare is not None:
            return fare, name
    return None, None


def probe(origin=ORIGIN, destination='NRT', timeout=FETCH_TIMEOUT):
    """각 레시피가 실제로 뭘 돌려주는지 진단한다. (배포 후 1회 확인용)

    개발 환경에서는 네이버로 나갈 수 없어 이 함수만이 실물 응답을 볼 수 있는
    유일한 창구다.
    """
    departure, return_date = iter_weekend_trips()[0]
    report = []
    for name, fetcher in FETCHERS:
        entry = {'recipe': name}
        try:
            fare, diagnostic = fetcher(origin, destination, departure, return_date, timeout)
            entry['fare'] = fare
            entry.update(diagnostic)
        except requests.RequestException as exc:
            entry['error'] = f'요청 실패: {exc}'
        report.append(entry)
    return {
        'origin': origin,
        'destination': destination,
        'departure': f'{departure:%Y-%m-%d}',
        'return': f'{return_date:%Y-%m-%d}',
        'url': naver_flight_url(origin, destination, departure, return_date),
        'recipes': report,
    }


def collect_offers(
    origin=ORIGIN,
    destinations=DESTINATIONS,
    trips=None,
    concurrency=FETCH_CONCURRENCY,
    max_price=MAX_PRICE,
    fail_fast_after=FAIL_FAST_AFTER,
):
    """(토요일 × 도시) 조합을 병렬로 훑어 가격 상한 이하 후보를 모은다.

    반환: (후보 목록, 통계). 통계의 `failed`/`succeeded`로 '진짜 싼 게 없는
    것'과 '수집 자체가 실패한 것'을 구분한다 — 이 둘을 뭉뚱그리면 네이버가
    막혔을 때도 "특가 없음"이라는 거짓 메시지가 나간다.

    한 건도 못 가져온 채 실패만 fail_fast_after건 쌓이면 남은 작업을 버린다.
    수백 건을 타임아웃마다 기다리면 스케줄 잡이 수십 분씩 매달리기 때문이다.
    """
    trips = trips if trips is not None else iter_weekend_trips()
    jobs = [
        (dest, departure, return_date)
        for dest in destinations
        for departure, return_date in trips
    ]

    offers = []
    stats = {'attempted': 0, 'succeeded': 0, 'failed': 0, 'total': len(jobs), 'aborted': False}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                search_fare, origin, dest['code'], departure, return_date,
            ): (dest, departure, return_date)
            for dest, departure, return_date in jobs
        }
        for future in as_completed(futures):
            dest, departure, return_date = futures[future]
            stats['attempted'] += 1
            try:
                fare, source = future.result()
            except Exception:
                fare, source = None, None

            if fare is None:
                stats['failed'] += 1
                if stats['succeeded'] == 0 and stats['failed'] >= fail_fast_after:
                    stats['aborted'] = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                continue

            stats['succeeded'] += 1
            if fare > max_price:
                continue
            offers.append({
                'price': fare,
                'city': dest['city'],
                'country': dest['country'],
                'code': dest['code'],
                'depart': departure,
                'return': return_date,
                'source': source,
                'url': naver_flight_url(origin, dest['code'], departure, return_date),
            })
    return offers, stats


def pick_recommendations(
    offers,
    max_price=MAX_PRICE,
    per_country=PER_COUNTRY,
    per_city=PER_CITY,
    limit=MAX_RESULTS,
):
    """싼 순서로 훑되 나라·도시 상한을 지켜 추천 목록을 만든다.

    나라 쿼터가 없으면 노선이 많은 일본·중국이 목록을 통째로 차지한다.
    도시 상한까지 두는 이유는 같은 도시가 날짜만 바꿔 그 나라 쿼터를 다
    먹어버리는 것을 막기 위함이다.
    """
    picked = []
    country_used = {}
    city_used = {}
    for offer in sorted(offers, key=lambda o: (o['price'], o['depart'], o['city'])):
        if offer['price'] > max_price:
            continue
        if country_used.get(offer['country'], 0) >= per_country:
            continue
        if city_used.get(offer['city'], 0) >= per_city:
            continue
        picked.append(offer)
        country_used[offer['country']] = country_used.get(offer['country'], 0) + 1
        city_used[offer['city']] = city_used.get(offer['city'], 0) + 1
        if limit and len(picked) >= limit:
            break
    return picked


def format_digest(offers, max_price=MAX_PRICE, origin=ORIGIN, searched=0, stats=None):
    """추천 목록을 텔레그램 HTML 메시지로 만든다.

    링크는 URL을 그대로 노출하지 않고 짧은 글자에 매달아 메시지를 짧게 지킨다.
    """
    stats = stats or {}
    if not offers:
        # 수집 자체가 실패한 경우를 '특가 없음'으로 뭉뚱그리면 안 된다.
        if stats.get('succeeded', 1) == 0:
            return (
                '⚠️ 항공권 수집 실패\n\n'
                f'네이버에서 가격을 한 건도 가져오지 못했습니다 '
                f'(시도 {stats.get("attempted", 0)}건).\n'
                '<code>/api/flights/probe?pw=1111&amp;telegram=1</code> 로 원인을 확인하세요.'
            )
        return (
            f'✈️ 토~월 2박3일 {max_price:,}원 이하 항공권\n\n'
            f'조건에 맞는 항공권을 찾지 못했습니다. (조합 {searched}건 확인)'
        )

    lines = [
        f'✈️ 토~월 2박3일 항공권 ({max_price:,}원 이하)',
        f'{ORIGIN_NAMES.get(origin, origin)} 출발 · 나라당 최대 {PER_COUNTRY}곳 · 싼 순',
    ]
    for index, offer in enumerate(offers, start=1):
        depart = offer['depart']
        back = offer['return']
        # 홍콩·괌처럼 도시가 곧 나라인 곳은 이름을 두 번 적지 않는다
        where = offer['city']
        if offer['country'] != offer['city']:
            where = f'{offer["country"]} {offer["city"]}'
        lines.append('')
        lines.append(
            f'{index}. <b>{offer["price"]:,}원</b> · {html.escape(where)}'
        )
        lines.append(
            f'   {depart:%m/%d}({WEEKDAY_KO[depart.weekday()]})~'
            f'{back:%m/%d}({WEEKDAY_KO[back.weekday()]}) · '
            f'<a href="{html.escape(offer["url"], quote=True)}">예약</a>'
        )
    return '\n'.join(lines)


def run_weekend_flight_digest(send_message, max_price=MAX_PRICE, origin=ORIGIN):
    """수집 → 쿼터 적용 → 텔레그램 발송까지 한 번에. (스케줄러가 부르는 진입점)

    send_message를 인자로 받는 이유는 이 모듈이 Flask/앱 설정에 의존하지 않게
    해 테스트에서 그대로 부를 수 있게 하기 위함이다.
    """
    trips = iter_weekend_trips()
    offers, stats = collect_offers(origin=origin, trips=trips, max_price=max_price)
    picked = pick_recommendations(offers, max_price=max_price)
    send_message(format_digest(
        picked,
        max_price=max_price,
        origin=origin,
        searched=stats['attempted'],
        stats=stats,
    ))
    return picked
