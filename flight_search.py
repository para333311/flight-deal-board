"""네이버 항공권 기반 '토~월 2박 3일' 주말 특가 검색.

월요일 하루만 휴가를 쓰는 일정이라 출발은 토요일, 귀국은 월요일로 고정한다.
앞으로 3개월 안의 모든 토요일 × 후보 도시를 훑어 가격 상한 이하만 남기고,
나라별 쿼터를 적용해 추천 목록을 만든 뒤 텔레그램으로 보낸다.

네트워크 계층은 네이버 GraphQL의 `minPricesByDate`를 직접 부르는 부분이라
개발 환경에서는 검증할 수 없었다(외부 접근이 막혀 있음). JS 번들 스캔으로
실제 쿼리문은 확보했지만 locationType/tripType 문자열 인자의 유효 값은
introspection 없이 알 수 없어, `calibrate_min_prices_by_date()`가 후보
조합을 실제로 돌려보고 찾는다. 어떤 조합이 통했는지는 `probe()`로 확인한다.

이 쿼리는 가격·날짜만 주고 시각(오전/오후)은 안 준다. 그래서 depart_hour/
return_hour는 항상 None이고, '오전 출발/오후 복귀' 조건은 아직 검증할 수
없다 — 시간 정보가 없다는 이유로 후보를 버리지는 않고 '시간 미확인'으로
표시한다 (matches_time_preference 참고).
"""

import calendar
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

# 배포가 실제로 반영됐는지 추측하지 않고 확인하기 위한 표식.
# probe()/introspect() 응답에 실려 나간다 — 값이 배포 전 커밋 때와 같으면
# Render가 아직 새 코드를 안 받은 것이다. 의미 있게 코드를 바꿀 때마다
# 문자열을 새로 바꿔둔다.
BUILD_MARKER = 'warm-session-2026-09-17'

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

# JS 번들 스캔(discover_queries)으로 확보한 실제 쿼리. 네이버 항공권 날짜별
# 캘린더 위젯이 쓰는 쿼리로, 도시 하나당 한 번만 불러도 여러 날짜의 최저가를
# 한꺼번에 돌려준다 (도시 수만큼만 부르면 되고, 도시×날짜만큼 부를 필요가
# 없다). 단 departureLocationType/arrivalLocationType(문자열)과 tripType의
# 실제 유효 값은 introspection이 막혀 있어 알 수 없어서 여러 후보를
# calibrate_min_prices_by_date()가 실제로 돌려보고 찾는다.
#
# timeCategories($timeCategories: [DepartureTimeCategory!]) 인자는 뺐다.
# 값을 안 보내도(선언만 있어도) 12개 조합 전부 line 10 col 21 —
# "DepartureTimeCategory" 타입명 정확히 그 위치 — 에서 동일하게
# GRAPHQL_VALIDATION_FAILED로 죽었다. 값과 무관하게 모든 조합이 똑같이
# 실패했다는 건 변수 값이 아니라 쿼리 '문서' 자체가 파싱 단계에서
# 거부됐다는 뜻이라, 이 타입명이 실제 스키마에 없는 것으로 보고 뺐다.
# (지금은 어차피 안 쓰던 필드라 손해가 없다 — 되찾으면 다시 붙이면 된다)
#
# 이 쿼리의 응답에는 시각(오전/오후) 정보가 없다 — departureDate/returnDate/
# minPrice/tripType뿐이다. 그래서 '오전 출발/오후 복귀' 조건은 아직 검증할
# 수 없고, depart_hour/return_hour는 항상 None(시간 미확인)으로 남는다.
NAVER_MIN_PRICES_BY_DATE_QUERY = """
query minPricesByDate(
  $departureLocationCode: String
  $departureLocationType: String
  $arrivalLocationCode: String
  $arrivalLocationType: String
  $departureDate: String
  $groupByDepartureDate: Boolean
  $isNonstop: Boolean
  $tripDays: [Int!]
  $tripType: String
) {
  minPricesByDate(
    departureLocationCode: $departureLocationCode
    departureLocationType: $departureLocationType
    arrivalLocationCode: $arrivalLocationCode
    arrivalLocationType: $arrivalLocationType
    departureDate: $departureDate
    groupByDepartureDate: $groupByDepartureDate
    isNonstop: $isNonstop
    tripDays: $tripDays
    tripType: $tripType
  ) {
    departureDate
    returnDate
    minPrice
    tripType
  }
}
"""

# locationType/tripType는 문자열 인자라 GraphQL이 유효값을 검증해주지 않는다
# (틀려도 에러가 아니라 그냥 빈 결과로 조용히 돌아온다). 그래서 실제로 결과가
# 돌아오는 조합을 찾을 때까지 후보를 순서대로 시도한다.
LOCATION_TYPE_CANDIDATES = ('AIRPORT', 'CITY', None)
TRIP_TYPE_CANDIDATES = ('RT', 'ROUND_TRIP', 'ROUND', None)

# 페이지가 어떤 이름으로 상태를 심어두는지 알아내기 위한 진단용 패턴
# (실제로는 __OTEL_* 뿐이었다 — 운임은 페이지에 안 실려 있고 XHR로 온다)
STATE_VAR_RE = re.compile(r'(?:window\.)?(__[A-Z0-9_]+__)\s*=')


def _min_prices_by_date_variables(
    origin, destination, trip_days, location_type=None, trip_type=None,
):
    variables = {
        'departureLocationCode': origin,
        'arrivalLocationCode': destination,
        'groupByDepartureDate': True,
        'isNonstop': False,
        'tripDays': [trip_days],
    }
    if location_type:
        variables['departureLocationType'] = location_type
        variables['arrivalLocationType'] = location_type
    if trip_type:
        variables['tripType'] = trip_type
    return variables


def _rows_from_min_prices_body(body):
    """minPricesByDate 응답에서 결과 목록을 꺼낸다. 형식이 안 맞으면 None."""
    data = (body or {}).get('data') or {}
    rows = data.get('minPricesByDate')
    return rows if isinstance(rows, list) else None


def warm_session(timeout=FETCH_TIMEOUT):
    """flight.naver.com을 먼저 방문해 세션 쿠키를 확보한다.

    쿠키 없이 GraphQL 엔드포인트를 바로 두드리면 실제 번들에 있는 필드인데도
    "Cannot query field" 류로 보이는 GRAPHQL_VALIDATION_FAILED가 난다 —
    게이트웨이가 세션 없는 요청은 축소된 스키마로 응답하는 것으로 보인다.
    진짜 브라우저처럼 페이지부터 들른 뒤 같은 세션으로 GraphQL을 부른다.
    """
    session = requests.Session()
    try:
        session.get(
            'https://flight.naver.com/', headers=NAVER_PAGE_HEADERS, timeout=timeout,
        )
    except requests.RequestException:
        pass
    return session


def fetch_min_prices_by_date(
    origin, destination, trip_days, location_type=None, trip_type=None,
    timeout=FETCH_TIMEOUT, session=None,
):
    """도시 하나의 날짜별 최저가 목록을 한 번에 받아온다.

    locationType/tripType은 문자열 인자라 틀려도 에러 없이 빈 결과만
    돌아오므로, 호출부(calibrate_min_prices_by_date/collect_offers)가 실제로
    행이 돌아오는 조합을 찾아 넘겨줘야 한다. session을 주지 않으면 쿠키 없는
    1회성 요청이 되므로, 보통은 warm_session()으로 만든 세션을 넘긴다.
    """
    variables = _min_prices_by_date_variables(
        origin, destination, trip_days, location_type, trip_type,
    )
    http = session or requests
    response = http.post(
        NAVER_GRAPHQL_URL,
        json={'query': NAVER_MIN_PRICES_BY_DATE_QUERY, 'variables': variables},
        headers=NAVER_PAGE_HEADERS,
        timeout=timeout,
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
    rows = _rows_from_min_prices_body(body)
    if not rows:
        diagnostic['error'] = '결과 없음'
        diagnostic['snippet'] = json.dumps(body, ensure_ascii=False)[:300]
        return None, diagnostic
    return rows, diagnostic


def calibrate_min_prices_by_date(
    origin=ORIGIN, destination='NRT', trip_days=TRIP_NIGHTS, timeout=FETCH_TIMEOUT,
    session=None,
):
    """locationType/tripType 후보 중 실제로 행을 돌려주는 조합을 찾는다.

    한 번 찾으면 이후 도시마다 다시 찾을 필요 없이 재사용한다(collect_offers
    가 이 함수를 한 번만 호출해 캐시한다). session을 안 주면 warm_session()
    으로 하나 만들어 쓴다.
    """
    session = session or warm_session(timeout)
    attempts = []
    for location_type in LOCATION_TYPE_CANDIDATES:
        for trip_type in TRIP_TYPE_CANDIDATES:
            try:
                rows, diagnostic = fetch_min_prices_by_date(
                    origin, destination, trip_days, location_type, trip_type,
                    timeout, session=session,
                )
            except requests.RequestException as exc:
                diagnostic = {'error': f'요청 실패: {exc}'}
                rows = None
            attempts.append({
                'location_type': location_type,
                'trip_type': trip_type,
                **diagnostic,
                'rows': len(rows) if rows else 0,
            })
            if rows:
                return {
                    'location_type': location_type,
                    'trip_type': trip_type,
                    'sample': rows[:5],
                }, attempts
    return None, attempts


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
    """locationType/tripType 조합을 실제로 돌려보고 뭐가 통하는지 진단한다.

    개발 환경에서는 네이버로 나갈 수 없어 이 함수만이 실물 응답을 볼 수 있는
    유일한 창구다. 성공하면 캘리브레이션 결과와 실제 표본 행을 돌려준다.
    """
    session = warm_session(timeout)
    calibration, attempts = calibrate_min_prices_by_date(
        origin=origin, destination=destination, timeout=timeout, session=session,
    )
    departure, return_date = iter_weekend_trips()[0]
    return {
        'build': BUILD_MARKER,
        'query': NAVER_MIN_PRICES_BY_DATE_QUERY,
        'cookies_obtained': len(session.cookies),
        'origin': origin,
        'destination': destination,
        'departure': f'{departure:%Y-%m-%d}',
        'return': f'{return_date:%Y-%m-%d}',
        'url': naver_flight_url(origin, destination, departure, return_date),
        'calibration': calibration,
        'attempts': attempts,
    }


def _parse_api_date(value):
    """minPricesByDate가 돌려주는 날짜 문자열을 date로 바꾼다.

    정확한 포맷(YYYYMMDD vs YYYY-MM-DD)이 introspection 없이는 확실치 않아
    둘 다 시도한다. 실물 응답을 보고 하나만 남겨도 되지만, 어느 쪽이든
    조용히 받아들이는 편이 더 안전하다.
    """
    if not value:
        return None
    for fmt in ('%Y%m%d', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value)[:10].replace('-', ''), '%Y%m%d').date()
        except ValueError:
            continue
    return None


def _parse_api_price(value):
    """minPrice가 문자열/정수 어느 쪽으로 와도 정수로 바꾼다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(',', '').strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def rows_to_offers(rows, dest, origin, trips, nights=TRIP_NIGHTS):
    """minPricesByDate 응답 행 중, 우리가 찾는 토요일 출발 건만 오퍼로 바꾼다.

    API가 기간 전체(우리가 원하는 3개월보다 넓거나 좁을 수 있음)를 돌려줄 수
    있으므로, 실제 조회 대상으로 잡은 trips(토요일 목록)에 있는 날짜만
    남긴다. 같은 날짜가 여러 행으로 와도 최저가만 남긴다.
    """
    wanted_departures = {departure for departure, _ in trips}
    best_by_date = {}
    for row in rows or []:
        departure = _parse_api_date(row.get('departureDate'))
        return_date = _parse_api_date(row.get('returnDate'))
        price = _parse_api_price(row.get('minPrice'))
        if departure is None or price is None:
            continue
        if departure not in wanted_departures:
            continue
        if return_date is not None and (return_date - departure).days != nights:
            continue
        if return_date is None:
            return_date = departure + timedelta(days=nights)
        if departure in best_by_date and best_by_date[departure]['price'] <= price:
            continue
        best_by_date[departure] = {
            'price': price,
            'city': dest['city'],
            'country': dest['country'],
            'code': dest['code'],
            'depart': departure,
            'return': return_date,
            'depart_hour': None,
            'return_hour': None,
            'time_ok': None,
            'source': 'graphql',
            'url': naver_flight_url(origin, dest['code'], departure, return_date),
        }
    return list(best_by_date.values())


def collect_offers(
    origin=ORIGIN,
    destinations=DESTINATIONS,
    trips=None,
    concurrency=FETCH_CONCURRENCY,
    max_price=MAX_PRICE,
    fail_fast_after=FAIL_FAST_AFTER,
    calibration=None,
):
    """도시별로 날짜별 최저가를 훑어 가격 상한 이하 후보를 모은다.

    minPricesByDate가 도시 하나당 한 번의 호출로 여러 날짜의 가격을 주기
    때문에, (도시 × 날짜) 조합이 아니라 도시 단위로만 요청한다.

    반환: (후보 목록, 통계). 통계의 `failed`/`succeeded`로 '진짜 싼 게 없는
    것'과 '수집 자체가 실패한 것'을 구분한다 — 이 둘을 뭉뚱그리면 네이버가
    막혔을 때도 "특가 없음"이라는 거짓 메시지가 나간다.

    한 건도 못 가져온 채 실패만 fail_fast_after건 쌓이면 남은 작업을 버린다.
    """
    trips = trips if trips is not None else iter_weekend_trips()
    stats = {
        'attempted': 0, 'succeeded': 0, 'failed': 0,
        'total': len(destinations), 'aborted': False,
    }

    # 세션을 한 번만 만들어 캘리브레이션과 도시별 요청 전체에서 재사용한다.
    # (쿠키 없이 GraphQL만 두드리면 게이트웨이가 필드를 모르는 것처럼
    # 취급한다 — warm_session 참고)
    session = warm_session()
    if calibration is None:
        calibration, _ = calibrate_min_prices_by_date(origin=origin, session=session)
    if calibration is None:
        stats['aborted'] = True
        return [], stats

    offers = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                fetch_min_prices_by_date, origin, dest['code'], TRIP_NIGHTS,
                calibration['location_type'], calibration['trip_type'],
                session=session,
            ): dest
            for dest in destinations
        }
        for future in as_completed(futures):
            dest = futures[future]
            stats['attempted'] += 1
            try:
                rows, _ = future.result()
            except Exception:
                rows = None

            if not rows:
                stats['failed'] += 1
                if stats['succeeded'] == 0 and stats['failed'] >= fail_fast_after:
                    stats['aborted'] = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                continue

            stats['succeeded'] += 1
            for offer in rows_to_offers(rows, dest, origin, trips):
                if offer['price'] <= max_price:
                    offers.append(offer)
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
