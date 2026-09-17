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
from urllib.parse import urljoin

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
# 2박 3일을 알차게 쓰려면 갈 때는 오전, 올 때는 오후 비행기여야 한다.
# 출발편은 이 시각 '전에' 출발, 복귀편은 이 시각 '이후에' 출발.
OUTBOUND_LATEST_HOUR = int(os.environ.get('FLIGHT_OUTBOUND_LATEST_HOUR', '12'))
INBOUND_EARLIEST_HOUR = int(os.environ.get('FLIGHT_INBOUND_EARLIEST_HOUR', '12'))
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
    r'(?:__NEXT_DATA__|__APOLLO_STATE__|__PRELOADED_STATE__|__NUXT__|__INITIAL_STATE__)'
    r'\s*=\s*({.*?})\s*[;<]',
    re.DOTALL,
)
# 페이지가 어떤 이름으로 상태를 심어두는지 알아내기 위한 진단용 패턴
STATE_VAR_RE = re.compile(r'(?:window\.)?(__[A-Z0-9_]+__)\s*=')


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


ROOT_FIELDS_QUERY = """
query {
  __schema {
    queryType {
      name
      fields {
        name
        args {
          name
          type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
        }
      }
    }
  }
}
"""

INPUT_TYPE_QUERY = """
query($name: String!) {
  __type(name: $name) {
    name
    kind
    inputFields {
      name
      type { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
    }
  }
}
"""

# 항공권 검색과 관련 있어 보이는 필드만 추려 응답을 짧게 유지한다
FLIGHT_FIELD_RE = re.compile(r'internation|domestic|flight|air|fare|schedule|list', re.I)


def _render_type_ref(type_ref):
    """introspection의 중첩된 타입 표현을 '[ItineraryInput!]!' 형태로 편다."""
    if not type_ref:
        return '?'
    kind = type_ref.get('kind')
    if kind == 'NON_NULL':
        return _render_type_ref(type_ref.get('ofType')) + '!'
    if kind == 'LIST':
        return '[' + _render_type_ref(type_ref.get('ofType')) + ']'
    return type_ref.get('name') or '?'


def _post_graphql(query, variables=None, timeout=FETCH_TIMEOUT):
    response = requests.post(
        NAVER_GRAPHQL_URL,
        json={'query': query, 'variables': variables or {}},
        headers=NAVER_PAGE_HEADERS,
        timeout=timeout,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {'_raw': response.text[:400]}


def introspect(timeout=FETCH_TIMEOUT, max_input_types=8):
    """네이버 GraphQL 스키마에서 실제 필드·입력타입 이름을 뽑아온다.

    요청 레시피를 추측으로 맞추는 대신 스키마에 직접 물어보기 위한 진단용.
    (개발 환경에서는 네이버로 나갈 수 없어 배포 후에만 실행된다)
    """
    status, body = _post_graphql(ROOT_FIELDS_QUERY, timeout=timeout)
    schema = (body.get('data') or {}).get('__schema') or {}
    query_type = schema.get('queryType') or {}
    fields = query_type.get('fields') or []

    if not fields:
        return {
            'introspection': 'unavailable',
            'status': status,
            'detail': json.dumps(body, ensure_ascii=False)[:500],
        }

    def describe(field):
        return {
            'name': field['name'],
            'args': [
                {'name': arg['name'], 'type': _render_type_ref(arg.get('type'))}
                for arg in (field.get('args') or [])
            ],
        }

    matching = [describe(f) for f in fields if FLIGHT_FIELD_RE.search(f['name'])]

    # 관심 필드의 인자로 쓰이는 입력 타입까지 펼쳐야 변수 구조를 알 수 있다
    wanted = []
    for field in matching:
        for arg in field['args']:
            name = arg['type'].strip('[]!')
            if name not in wanted and name not in ('String', 'Int', 'Boolean', 'Float', 'ID'):
                wanted.append(name)

    input_types = {}
    for name in wanted[:max_input_types]:
        _, type_body = _post_graphql(INPUT_TYPE_QUERY, {'name': name}, timeout=timeout)
        type_info = (type_body.get('data') or {}).get('__type') or {}
        if type_info.get('inputFields'):
            input_types[name] = [
                {'name': f['name'], 'type': _render_type_ref(f.get('type'))}
                for f in type_info['inputFields']
            ]

    return {
        'introspection': 'ok',
        'status': status,
        'query_type': query_type.get('name'),
        'field_count': len(fields),
        'all_field_names': [f['name'] for f in fields],
        'matching_fields': matching,
        'input_types': input_types,
    }


SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
# 번들에 문자열로 박혀 있는 GraphQL 오퍼레이션. introspection이 꺼져 있어도
# 브라우저가 쓰는 쿼리문 자체는 JS 번들에 그대로 실려 온다.
GQL_OPERATION_RE = re.compile(r'\b(query|mutation)\s+([A-Za-z_]\w*)\s*[({]')
OPERATION_NAME_RE = re.compile(r'operationName\s*[:=]\s*["\']([A-Za-z_]\w*)["\']')
# Apollo는 gql 템플릿을 AST로 컴파일해 넣기 때문에 오퍼레이션 이름이
# 평문이 아니라 {kind:"Name",value:"..."} 노드로 박힌다.
AST_OPERATION_RE = re.compile(
    r'operation:\s*"(query|mutation)"\s*,\s*name:\s*\{\s*kind:\s*"Name"\s*,\s*value:\s*"(\w+)"'
)

# 운임 검색 쿼리를 곁가지 쿼리보다 먼저 집어내기 위한 점수표.
# 상한이 낮았을 때 airportDetailList·promotions 같은 게 자리를 다 차지했다.
OPERATION_SCORES = (
    ('internation', 4), ('schedule', 4), ('fare', 3), ('shopping', 3),
    ('search', 3), ('flight', 2), ('price', 2), ('list', 1), ('domestic', 1),
)
OPERATION_PENALTIES = (
    'promotion', 'airportdetail', 'subscription', 'ads', 'banner', 'isdirect',
    'delete', 'review', 'hotel', 'rentcar', 'benefit', 'coupon', 'notice',
)


def score_operation_name(name):
    """이름만 보고 '운임 검색 쿼리일 가능성'을 점수로 매긴다."""
    lowered = name.lower()
    if any(bad in lowered for bad in OPERATION_PENALTIES):
        return 0
    return sum(weight for keyword, weight in OPERATION_SCORES if keyword in lowered)


def _extract_graphql_docs(text, interesting=None, doc_chars=2500, limit=8):
    """번들 텍스트에서 GraphQL 오퍼레이션 정의를 잘라낸다.

    interesting을 주면 그 패턴에 맞는 이름만, 없으면 점수가 높은 순으로
    고른다. (점수는 score_operation_name 참고)
    """
    found = []
    for match in GQL_OPERATION_RE.finditer(text):
        name = match.group(2)
        if interesting is not None and not interesting.search(name):
            continue
        score = score_operation_name(name)
        if interesting is None and score <= 0:
            continue
        found.append({
            'operation': match.group(1),
            'name': name,
            'score': score,
            'doc': text[match.start():match.start() + doc_chars],
        })

    if interesting is None:
        found.sort(key=lambda d: -d['score'])
    return found[:limit]


def discover_queries(
    origin=ORIGIN,
    destination='NRT',
    timeout=FETCH_TIMEOUT,
    max_scripts=30,
    max_bytes=6_000_000,
    wanted=None,
):
    """네이버 항공권 JS 번들에서 실제 GraphQL 쿼리문을 찾아낸다.

    introspection이 막혀 있고 페이지에도 데이터가 없을 때 남은 유일한
    자동 경로다. 브라우저가 보내는 쿼리문은 번들에 문자열로 들어 있다.
    """
    page_url = naver_flight_url(
        origin, destination, *iter_weekend_trips()[0],
    )
    page = requests.get(page_url, headers=NAVER_PAGE_HEADERS, timeout=timeout)
    sources = [urljoin(page_url, src) for src in SCRIPT_SRC_RE.findall(page.text)]

    def collect_names(text):
        names = set(OPERATION_NAME_RE.findall(text))
        names.update(name for _, name in AST_OPERATION_RE.findall(text))
        names.update(name for _, name in GQL_OPERATION_RE.findall(text))
        return names

    operation_names = collect_names(page.text)
    docs = _extract_graphql_docs(page.text)

    scanned = 0
    budget = max_bytes
    errors = []
    for src in sources[:max_scripts]:
        if budget <= 0:
            break
        try:
            script = requests.get(src, headers=NAVER_PAGE_HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            errors.append(f'{src.rsplit("/", 1)[-1]}: {exc}')
            continue
        scanned += 1
        budget -= len(script.content)
        body = script.text
        operation_names.update(collect_names(body))
        docs.extend(_extract_graphql_docs(body))

    # 원하는 오퍼레이션을 콕 집어 볼 수 있게 (?op=이름)
    if wanted:
        docs = [d for d in docs if wanted.lower() in d['name'].lower()]

    # 이름이 같은 중복을 없애고 점수 높은 순으로 추린다
    best = {}
    for doc in docs:
        if doc['name'] not in best or len(doc['doc']) > len(best[doc['name']]['doc']):
            best[doc['name']] = doc
    ranked = sorted(best.values(), key=lambda d: -d['score'])[:8]

    return {
        'page_status': page.status_code,
        'scripts_found': len(sources),
        'scripts_scanned': scanned,
        'operation_count': len(operation_names),
        'operation_names': sorted(operation_names),
        'top_candidates': [
            {'name': d['name'], 'score': d['score']}
            for d in sorted(best.values(), key=lambda d: -d['score'])[:15]
        ],
        'graphql_docs': ranked,
        'errors': errors[:5],
    }


def inspect_page(origin=ORIGIN, destination='NRT', timeout=FETCH_TIMEOUT):
    """검색 페이지가 어떤 이름으로 상태 JSON을 심는지 확인한다. (폴백 경로 진단)"""
    departure, return_date = iter_weekend_trips()[0]
    url = naver_flight_url(origin, destination, departure, return_date)
    response = requests.get(url, headers=NAVER_PAGE_HEADERS, timeout=timeout)
    return {
        'status': response.status_code,
        'bytes': len(response.content),
        'state_vars': sorted(set(STATE_VAR_RE.findall(response.text))),
    }


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


def matches_time_preference(
    outbound_hour,
    inbound_hour,
    outbound_latest=OUTBOUND_LATEST_HOUR,
    inbound_earliest=INBOUND_EARLIEST_HOUR,
):
    """갈 때 오전, 올 때 오후여야 2박 3일을 온전히 쓴다.

    시각을 모르면 None을 돌려준다. 이때 후보를 버리지는 않는다 — 시간 정보를
    못 얻었다는 이유로 전부 날리면 추천이 통째로 비기 때문이다. 대신 메시지에
    '시간 미확인'으로 표시해 검증된 것과 구분한다.
    """
    if outbound_hour is None or inbound_hour is None:
        return None
    return outbound_hour < outbound_latest and inbound_hour >= inbound_earliest


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
        # 오전 출발/오후 복귀가 아닌 게 확인된 편은 뺀다 (미확인은 통과)
        if offer.get('time_ok') is False:
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
        f'{ORIGIN_NAMES.get(origin, origin)} 출발 · 갈 때 오전/올 때 오후 · '
        f'나라당 최대 {PER_COUNTRY}곳 · 싼 순',
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
        out_hour, in_hour = offer.get('depart_hour'), offer.get('return_hour')
        if out_hour is None or in_hour is None:
            when = '시간 미확인'
        else:
            when = f'{out_hour:02d}시 출발 / {in_hour:02d}시 복귀'
        lines.append(
            f'   {depart:%m/%d}({WEEKDAY_KO[depart.weekday()]})~'
            f'{back:%m/%d}({WEEKDAY_KO[back.weekday()]}) · {when} · '
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
