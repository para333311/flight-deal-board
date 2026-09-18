import re
import unittest
from datetime import date
from unittest.mock import Mock, patch

import flight_search


def _offer(price, city, country, day=10, code=None):
    departure = date(2026, 10, day)
    return {
        "price": price,
        "city": city,
        "country": country,
        "code": code or city[:3].upper(),
        "depart": departure,
        "return": date(2026, 10, day + 2),
        "source": "graphql",
        "url": "https://flight.naver.com/x",
    }


class WeekendTripTests(unittest.TestCase):
    def test_departures_are_saturdays_and_returns_are_mondays(self):
        # 2026-09-17은 목요일
        trips = flight_search.iter_weekend_trips(today=date(2026, 9, 17), months=1)

        self.assertTrue(trips)
        for departure, return_date in trips:
            self.assertEqual(departure.weekday(), 5, f"{departure} 는 토요일이 아님")
            self.assertEqual(return_date.weekday(), 0, f"{return_date} 는 월요일이 아님")
            self.assertEqual((return_date - departure).days, 2)  # 2박 3일

    def test_first_trip_is_the_upcoming_saturday(self):
        trips = flight_search.iter_weekend_trips(today=date(2026, 9, 17), months=1)
        self.assertEqual(trips[0][0], date(2026, 9, 19))

    def test_today_being_saturday_skips_same_day_departure(self):
        """오늘이 토요일이면 당일 출발은 의미가 없으니 다음 주부터 본다."""
        trips = flight_search.iter_weekend_trips(today=date(2026, 9, 19), months=1)
        self.assertEqual(trips[0][0], date(2026, 9, 26))

    def test_range_stops_at_three_months_out(self):
        trips = flight_search.iter_weekend_trips(today=date(2026, 9, 17), months=3)
        self.assertLessEqual(trips[-1][0], date(2026, 12, 17))
        self.assertGreater(trips[-1][0], date(2026, 12, 10))
        # 3개월이면 대략 13주
        self.assertGreaterEqual(len(trips), 12)
        self.assertLessEqual(len(trips), 14)


class ExtractMinFareTests(unittest.TestCase):
    def test_finds_fare_regardless_of_nesting(self):
        payload = {"data": {"list": [{"fare": {"adultFare": 398000}}]}}
        self.assertEqual(flight_search.extract_min_fare(payload), 398000)

    def test_picks_the_cheapest_candidate(self):
        payload = {"results": [{"totalFare": 512000}, {"totalFare": 268000}]}
        self.assertEqual(flight_search.extract_min_fare(payload), 268000)

    def test_accepts_comma_formatted_string_prices(self):
        self.assertEqual(flight_search.extract_min_fare({"price": "349,000"}), 349000)

    def test_ignores_numbers_that_are_not_fares(self):
        """좌석수·소요시간·타임스탬프가 가격으로 잡히면 안 된다."""
        payload = {
            "seatCount": 3,
            "durationMinutes": 155,
            "departureTime": 1760000000,
            "fare": {"adultFare": 271000},
        }
        self.assertEqual(flight_search.extract_min_fare(payload), 271000)

    def test_ignores_out_of_range_values_under_a_fare_key(self):
        # 3만원 미만 / 500만원 초과는 왕복 항공권 값으로 보지 않는다
        self.assertIsNone(flight_search.extract_min_fare({"fare": 900}))
        self.assertIsNone(flight_search.extract_min_fare({"totalFare": 99000000}))

    def test_returns_none_when_nothing_matches(self):
        self.assertIsNone(flight_search.extract_min_fare({"errors": ["nope"]}))
        self.assertIsNone(flight_search.extract_min_fare({}))

    def test_booleans_under_fare_key_are_not_prices(self):
        self.assertIsNone(flight_search.extract_min_fare({"hasFare": True}))


class RecommendationTests(unittest.TestCase):
    def test_country_quota_caps_japan_and_china_flooding(self):
        """일본·중국이 목록을 통째로 먹지 않게 나라당 3개까지만 뽑는다."""
        offers = [
            _offer(100000 + i * 1000, city, "일본")
            for i, city in enumerate(["도쿄", "오사카", "후쿠오카", "삿포로", "나고야"])
        ] + [
            _offer(200000 + i * 1000, city, "중국")
            for i, city in enumerate(["상하이", "베이징", "칭다오", "항저우"])
        ] + [
            _offer(300000, "방콕", "태국"),
        ]

        picked = flight_search.pick_recommendations(offers, per_country=3)

        countries = [o["country"] for o in picked]
        self.assertEqual(countries.count("일본"), 3)
        self.assertEqual(countries.count("중국"), 3)
        self.assertIn("태국", countries)

    def test_same_city_on_different_dates_does_not_eat_the_country_quota(self):
        offers = [
            _offer(150000, "도쿄", "일본", day=3),
            _offer(160000, "도쿄", "일본", day=10),
            _offer(170000, "도쿄", "일본", day=17),
            _offer(180000, "오사카", "일본"),
            _offer(190000, "후쿠오카", "일본"),
        ]

        picked = flight_search.pick_recommendations(offers, per_country=3, per_city=1)

        self.assertEqual([o["city"] for o in picked], ["도쿄", "오사카", "후쿠오카"])
        # 도쿄는 가장 싼 날짜만 남는다
        self.assertEqual(picked[0]["depart"], date(2026, 10, 3))

    def test_drops_offers_over_the_price_cap(self):
        offers = [_offer(399000, "다낭", "베트남"), _offer(400001, "세부", "필리핀")]

        picked = flight_search.pick_recommendations(offers, max_price=400000)

        self.assertEqual([o["city"] for o in picked], ["다낭"])

    def test_exactly_at_the_cap_is_kept(self):
        picked = flight_search.pick_recommendations([_offer(400000, "다낭", "베트남")])
        self.assertEqual(len(picked), 1)

    def test_sorted_cheapest_first(self):
        offers = [
            _offer(300000, "방콕", "태국"),
            _offer(100000, "도쿄", "일본"),
            _offer(200000, "타이베이", "대만"),
        ]

        prices = [o["price"] for o in flight_search.pick_recommendations(offers)]

        self.assertEqual(prices, [100000, 200000, 300000])

    def test_limit_caps_total_results(self):
        offers = [
            _offer(100000 + i, f"도시{i}", f"나라{i}") for i in range(30)
        ]
        self.assertEqual(len(flight_search.pick_recommendations(offers, limit=5)), 5)


class FormatDigestTests(unittest.TestCase):
    def test_includes_price_country_city_dates_and_link(self):
        message = flight_search.format_digest([_offer(271000, "후쿠오카", "일본")])

        self.assertIn("271,000원", message)
        self.assertIn("일본 후쿠오카", message)
        self.assertIn("10/10(토)~10/12(월)", message)
        self.assertIn('<a href="https://flight.naver.com/x">예약</a>', message)

    def test_empty_result_says_so_instead_of_sending_a_blank_list(self):
        message = flight_search.format_digest(
            [], searched=442, stats={"attempted": 442, "succeeded": 442},
        )

        self.assertIn("찾지 못했습니다", message)
        self.assertIn("442", message)

    def test_collection_failure_is_not_reported_as_no_deals(self):
        """수집 장애를 '특가 없음'으로 보내면 거짓말이 된다."""
        message = flight_search.format_digest(
            [], stats={"attempted": 15, "succeeded": 0, "failed": 15, "aborted": True},
        )

        self.assertIn("수집 실패", message)
        self.assertNotIn("찾지 못했습니다", message)
        self.assertIn("probe", message)

    def test_city_that_is_its_own_country_is_not_printed_twice(self):
        message = flight_search.format_digest([_offer(295000, "홍콩", "홍콩")])

        self.assertIn("· 홍콩", message)
        self.assertNotIn("홍콩 홍콩", message)

    def test_origin_is_shown_in_korean(self):
        message = flight_search.format_digest([_offer(100000, "도쿄", "일본")], origin="ICN")
        self.assertIn("인천 출발", message)

    def test_escapes_html_special_characters(self):
        offer = _offer(100000, "a<b>", "c&d")
        message = flight_search.format_digest([offer])

        self.assertNotIn("a<b>", message)
        self.assertIn("a&lt;b&gt;", message)
        self.assertIn("c&amp;d", message)


class NaverUrlTests(unittest.TestCase):
    def test_builds_round_trip_search_url(self):
        url = flight_search.naver_flight_url(
            "ICN", "FUK", date(2026, 10, 10), date(2026, 10, 12)
        )
        self.assertEqual(
            url,
            "https://flight.naver.com/flights/international/"
            "ICN-FUK-20261010/FUK-ICN-20261012?adult=1&fareType=Y",
        )


class ParseApiValueTests(unittest.TestCase):
    def test_parses_compact_date(self):
        self.assertEqual(flight_search._parse_api_date("20261010"), date(2026, 10, 10))

    def test_parses_hyphenated_date(self):
        self.assertEqual(flight_search._parse_api_date("2026-10-10"), date(2026, 10, 10))

    def test_none_and_empty_date_are_not_a_crash(self):
        self.assertIsNone(flight_search._parse_api_date(None))
        self.assertIsNone(flight_search._parse_api_date(""))

    def test_parses_int_and_comma_string_prices(self):
        self.assertEqual(flight_search._parse_api_price(271000), 271000)
        self.assertEqual(flight_search._parse_api_price("271,000"), 271000)

    def test_booleans_and_junk_are_not_prices(self):
        self.assertIsNone(flight_search._parse_api_price(True))
        self.assertIsNone(flight_search._parse_api_price("free"))
        self.assertIsNone(flight_search._parse_api_price(None))


class RowsToOffersTests(unittest.TestCase):
    DEST = {"code": "FUK", "city": "후쿠오카", "country": "일본"}
    TRIPS = [(date(2026, 10, 10), date(2026, 10, 12)), (date(2026, 10, 17), date(2026, 10, 19))]

    def test_keeps_only_rows_matching_our_saturdays(self):
        rows = [
            {"departureDate": "20261010", "returnDate": "20261012", "minPrice": 150000},
            # 우리가 조회하지 않은 날짜 (평일 출발) — API가 넓은 기간을 줬을 때 걸러야 함
            {"departureDate": "20261013", "returnDate": "20261015", "minPrice": 90000},
        ]
        offers = flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS)

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["depart"], date(2026, 10, 10))
        self.assertEqual(offers[0]["price"], 150000)

    def test_drops_rows_whose_trip_length_does_not_match(self):
        """tripDays=2로 요청해도 응답에 다른 일정이 섞여 오면 걸러낸다."""
        rows = [{"departureDate": "20261010", "returnDate": "20261015", "minPrice": 150000}]
        offers = flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS)
        self.assertEqual(offers, [])

    def test_missing_return_date_falls_back_to_nights_later(self):
        rows = [{"departureDate": "20261010", "minPrice": 150000}]
        offers = flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS)
        self.assertEqual(offers[0]["return"], date(2026, 10, 12))

    def test_keeps_cheapest_when_a_date_appears_twice(self):
        rows = [
            {"departureDate": "20261010", "returnDate": "20261012", "minPrice": 200000},
            {"departureDate": "20261010", "returnDate": "20261012", "minPrice": 150000},
        ]
        offers = flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["price"], 150000)

    def test_time_fields_are_unknown_not_guessed(self):
        """이 쿼리는 시각을 안 준다 — 모르는 걸 아는 척하면 안 된다."""
        rows = [{"departureDate": "20261010", "returnDate": "20261012", "minPrice": 150000}]
        offer = flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS)[0]
        self.assertIsNone(offer["depart_hour"])
        self.assertIsNone(offer["return_hour"])
        self.assertIsNone(offer["time_ok"])

    def test_rows_missing_price_or_date_are_skipped(self):
        rows = [
            {"departureDate": "20261010", "returnDate": "20261012"},
            {"returnDate": "20261012", "minPrice": 150000},
        ]
        self.assertEqual(flight_search.rows_to_offers(rows, self.DEST, "ICN", self.TRIPS), [])


class CalibrateMinPricesByDateTests(unittest.TestCase):
    def setUp(self):
        # warm_session()의 실제 네트워크 시도를 막아 테스트를 빠르고
        # 결정적으로 만든다 — fetch_min_prices_by_date 자체는 모킹하므로
        # 세션 내용은 테스트와 무관하다.
        patcher = patch.object(flight_search, "warm_session", return_value=Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stops_at_the_first_combo_that_returns_rows(self):
        calls = []

        def fake_fetch(origin, destination, trip_days, location_type, trip_type, timeout, **kwargs):
            calls.append((location_type, trip_type))
            if location_type == "CITY" and trip_type == "RT":
                return [{"departureDate": "20261010", "minPrice": 1000}], {"status": 200}
            return None, {"status": 200, "error": "결과 없음"}

        with patch.object(flight_search, "fetch_min_prices_by_date", side_effect=fake_fetch):
            calibration, attempts = flight_search.calibrate_min_prices_by_date()

        self.assertEqual(calibration["location_type"], "CITY")
        self.assertEqual(calibration["trip_type"], "RT")
        self.assertEqual(calibration["sample"][0]["minPrice"], 1000)
        # CITY/RT에서 멈췄으니 그 이후 조합은 시도하지 않는다
        self.assertNotIn(("CITY", "ROUND_TRIP"), calls)

    def test_reports_every_attempt_when_nothing_works(self):
        with patch.object(
            flight_search, "fetch_min_prices_by_date",
            return_value=(None, {"status": 400, "error": "결과 없음"}),
        ):
            calibration, attempts = flight_search.calibrate_min_prices_by_date()

        self.assertIsNone(calibration)
        expected = len(flight_search.LOCATION_TYPE_CANDIDATES) * len(flight_search.TRIP_TYPE_CANDIDATES)
        self.assertEqual(len(attempts), expected)

    def test_network_error_in_one_combo_does_not_abort_calibration(self):
        def fake_fetch(origin, destination, trip_days, location_type, trip_type, timeout, **kwargs):
            if location_type == "AIRPORT":
                raise flight_search.requests.RequestException("boom")
            return [{"departureDate": "20261010", "minPrice": 1000}], {"status": 200}

        with patch.object(flight_search, "fetch_min_prices_by_date", side_effect=fake_fetch):
            calibration, attempts = flight_search.calibrate_min_prices_by_date()

        self.assertIsNotNone(calibration)
        self.assertTrue(any("요청 실패" in str(a.get("error", "")) for a in attempts))


def _type_ref(kind, name=None, of_type=None):
    return {"kind": kind, "name": name, "ofType": of_type}


class IntrospectionTests(unittest.TestCase):
    """요청 레시피를 추측으로 맞출 수 없으니 스키마에 직접 물어본다."""

    ROOT = {
        "data": {
            "__schema": {
                "queryType": {
                    "name": "Query",
                    "fields": [
                        {
                            "name": "internationalFlightList",
                            "args": [
                                {
                                    "name": "itinerary",
                                    "type": _type_ref(
                                        "NON_NULL",
                                        of_type=_type_ref(
                                            "LIST",
                                            of_type=_type_ref(
                                                "INPUT_OBJECT", "FlightItineraryInput"
                                            ),
                                        ),
                                    ),
                                },
                                {"name": "adult", "type": _type_ref("SCALAR", "Int")},
                            ],
                        },
                        {"name": "unrelatedThing", "args": []},
                    ],
                }
            }
        }
    }
    INPUT = {
        "data": {
            "__type": {
                "name": "FlightItineraryInput",
                "kind": "INPUT_OBJECT",
                "inputFields": [
                    {"name": "departureAirport", "type": _type_ref("SCALAR", "String")},
                ],
            }
        }
    }

    def _fake_post(self, query, variables=None, timeout=None):
        return (200, self.ROOT) if not variables else (200, self.INPUT)

    def test_reports_real_field_names_and_expands_input_types(self):
        with patch.object(flight_search, "_post_graphql", side_effect=self._fake_post):
            report = flight_search.introspect()

        self.assertEqual(report["introspection"], "ok")
        self.assertEqual(report["query_type"], "Query")
        self.assertIn("internationalFlightList", report["all_field_names"])
        # 항공권과 무관한 필드는 추려낸다
        self.assertEqual([f["name"] for f in report["matching_fields"]],
                         ["internationalFlightList"])
        args = report["matching_fields"][0]["args"]
        self.assertEqual(args[0]["type"], "[FlightItineraryInput]!")
        self.assertIn("FlightItineraryInput", report["input_types"])

    def test_scalar_args_do_not_trigger_input_type_lookups(self):
        with patch.object(flight_search, "_post_graphql", side_effect=self._fake_post):
            report = flight_search.introspect()

        self.assertNotIn("Int", report["input_types"])

    def test_says_unavailable_when_introspection_is_disabled(self):
        blocked = (400, {"errors": [{"message": "introspection is disabled"}]})
        with patch.object(flight_search, "_post_graphql", return_value=blocked):
            report = flight_search.introspect()

        self.assertEqual(report["introspection"], "unavailable")
        self.assertIn("introspection is disabled", report["detail"])


class TimePreferenceTests(unittest.TestCase):
    """2박 3일을 알차게 쓰려면 갈 때 오전, 올 때 오후여야 한다."""

    def test_morning_out_and_afternoon_back_is_accepted(self):
        self.assertTrue(flight_search.matches_time_preference(8, 19))

    def test_afternoon_departure_is_rejected(self):
        self.assertFalse(flight_search.matches_time_preference(15, 19))

    def test_morning_return_is_rejected(self):
        self.assertFalse(flight_search.matches_time_preference(8, 9))

    def test_boundary_noon(self):
        # 정오 출발은 '오전'이 아니고, 정오 복귀는 '오후'로 친다
        self.assertFalse(flight_search.matches_time_preference(12, 19))
        self.assertTrue(flight_search.matches_time_preference(11, 12))

    def test_unknown_times_are_undecided_not_rejected(self):
        self.assertIsNone(flight_search.matches_time_preference(None, 19))
        self.assertIsNone(flight_search.matches_time_preference(8, None))


class TimeFilteringInPicksTests(unittest.TestCase):
    def test_offers_known_to_violate_the_time_rule_are_dropped(self):
        bad = dict(_offer(100000, "도쿄", "일본"), time_ok=False)
        good = dict(_offer(200000, "오사카", "일본"), time_ok=True)

        picked = flight_search.pick_recommendations([bad, good])

        self.assertEqual([o["city"] for o in picked], ["오사카"])

    def test_unknown_time_offers_still_get_through(self):
        """시간 정보를 못 얻었다고 전부 버리면 추천이 통째로 빈다."""
        unknown = dict(_offer(100000, "도쿄", "일본"), time_ok=None)

        picked = flight_search.pick_recommendations([unknown])

        self.assertEqual(len(picked), 1)

    def test_message_shows_times_when_known(self):
        offer = dict(_offer(150000, "후쿠오카", "일본"), depart_hour=8, return_hour=19)
        message = flight_search.format_digest([offer])

        self.assertIn("08시 출발 / 19시 복귀", message)

    def test_message_marks_unknown_times_instead_of_pretending(self):
        message = flight_search.format_digest([_offer(150000, "후쿠오카", "일본")])

        self.assertIn("시간 미확인", message)


class BundleDiscoveryTests(unittest.TestCase):
    """introspection이 막혀 있어도 쿼리문은 JS 번들에 문자열로 실려 온다."""

    def test_extracts_flight_related_operations_only(self):
        bundle = (
            'foo(){return e.query(query getInternationalFlightList($a: Int){ list { fare } })}'
            'bar(){return e.query(query getUserProfile($b: Int){ name })}'
        )
        docs = flight_search._extract_graphql_docs(
            bundle, re.compile(r"internation|flight", re.I)
        )

        self.assertEqual([d["name"] for d in docs], ["getInternationalFlightList"])
        self.assertIn("fare", docs[0]["doc"])

    def test_search_queries_outrank_side_queries(self):
        """상한이 낮을 때 airportDetailList 같은 곁가지가 자리를 다 먹었었다."""
        bundle = (
            "query airportDetailList($a: Int){ iataCode }"
            "query international_promotions($b: Int){ promotions }"
            "query getInternationalScheduleFareList($c: Int){ fare }"
        )

        docs = flight_search._extract_graphql_docs(bundle, limit=3)

        self.assertEqual(docs[0]["name"], "getInternationalScheduleFareList")
        # 곁가지로 알려진 것들은 아예 후보에서 빠진다
        self.assertNotIn("airportDetailList", [d["name"] for d in docs])
        self.assertNotIn("international_promotions", [d["name"] for d in docs])

    def test_scoring_prefers_fare_search_names(self):
        score = flight_search.score_operation_name
        self.assertGreater(
            score("getInternationalScheduleList"), score("getBookings")
        )
        # 곁가지로 확인된 이름은 0점 처리해 후보에서 제외
        self.assertEqual(score("airportDetailList"), 0)
        self.assertEqual(score("international_promotions"), 0)

    def test_collects_names_from_apollo_ast_nodes(self):
        """Apollo는 쿼리를 AST로 컴파일해 넣어서 이름이 평문으로 안 남는다."""
        bundle = (
            '{kind:"Document",definitions:[{kind:"OperationDefinition",'
            'operation:"query",name:{kind:"Name",value:"getInternationalList"}}]}'
        )
        names = [n for _, n in flight_search.AST_OPERATION_RE.findall(bundle)]

        self.assertEqual(names, ["getInternationalList"])

    def test_collects_operation_names_from_scripts(self):
        page_html = '<script src="/js/main.js"></script>'
        bundle = 'operationName:"getInternationalList",x=1,operationName:"getAirportList"'

        responses = {
            "page": Mock(status_code=200, text=page_html, content=b"x"),
            "bundle": Mock(status_code=200, text=bundle, content=bundle.encode()),
        }

        def fake_get(url, **kwargs):
            return responses["bundle"] if url.endswith("main.js") else responses["page"]

        with patch.object(flight_search.requests, "get", side_effect=fake_get):
            report = flight_search.discover_queries()

        self.assertEqual(report["scripts_found"], 1)
        self.assertEqual(report["scripts_scanned"], 1)
        self.assertIn("getInternationalList", report["operation_names"])
        self.assertIn("getAirportList", report["operation_names"])

    def test_one_broken_script_does_not_abort_the_scan(self):
        page_html = '<script src="/a.js"></script><script src="/b.js"></script>'

        def fake_get(url, **kwargs):
            if url.endswith("a.js"):
                raise flight_search.requests.RequestException("boom")
            if url.endswith("b.js"):
                return Mock(status_code=200, text='operationName:"getFlight"', content=b"x")
            return Mock(status_code=200, text=page_html, content=b"x")

        with patch.object(flight_search.requests, "get", side_effect=fake_get):
            report = flight_search.discover_queries()

        self.assertEqual(report["scripts_scanned"], 1)
        self.assertIn("getFlight", report["operation_names"])
        self.assertTrue(report["errors"])


class EndpointDiscoveryTests(unittest.TestCase):
    """필드가 번들에 있는데도 '모르는 필드'라면 보통 엔드포인트가 틀린 것이다."""

    def test_finds_absolute_and_relative_graphql_urls(self):
        page_html = '<script src="/js/main.js"></script>'
        bundle = (
            'var a="https://airline-api.naver.com/graphql";'
            'var b="https://flight-api.naver.com/v1/graphql";'
            'fetch("/api/flight/graphql",{})'
        )

        def fake_get(url, **kwargs):
            text = bundle if url.endswith('main.js') else page_html
            return Mock(status_code=200, text=text, content=text.encode())

        with patch.object(flight_search.requests, "get", side_effect=fake_get):
            found = flight_search.discover_endpoints()

        self.assertIn("https://airline-api.naver.com/graphql", found["absolute"])
        self.assertIn("https://flight-api.naver.com/v1/graphql", found["absolute"])
        self.assertIn("/api/flight/graphql", found["relative"])
        # 상대 경로는 절대 주소로 바뀌어 후보에 들어간다
        self.assertTrue(
            any(c.endswith("/api/flight/graphql") and c.startswith("https://")
                for c in found["candidates"])
        )

    def test_builds_candidates_from_every_naver_host_found(self):
        """실제 주소는 번들에 안 박혀 있고 런타임 설정으로 주입된다.
        그래서 등장한 네이버 호스트에 흔한 경로를 붙여 조합해 시도한다."""
        page_html = '<script src="/js/main.js"></script>'
        bundle = (
            'var a="https://api.flight.naver.com/x";'
            'var b="https://airline-api.naver.com/y";'
            'var c="https://nfront.pstatic.net/z";'
        )

        def fake_get(url, **kwargs):
            text = bundle if url.endswith('main.js') else page_html
            return Mock(status_code=200, text=text, content=text.encode())

        with patch.object(flight_search.requests, "get", side_effect=fake_get):
            found = flight_search.discover_endpoints()

        # 다단계 서브도메인도 잡아야 한다
        self.assertIn("api.flight.naver.com", found["naver_hosts"])
        # 네이버가 아닌 호스트(pstatic)는 후보에 넣지 않는다
        self.assertFalse(any("pstatic" in c for c in found["candidates"]))
        self.assertIn("https://api.flight.naver.com/graphql", found["candidates"])
        # 항공권 관련 호스트가 앞쪽에 온다
        self.assertTrue(found["candidates"][0].startswith("https://api.flight.naver.com"))

    def test_candidate_count_is_capped_to_avoid_hammering(self):
        """네이버가 이미 503을 준 적이 있어 요청 폭주를 막아야 한다."""
        page_html = '<script src="/js/main.js"></script>'
        bundle = ''.join(f'var x{i}="https://h{i}.naver.com/a";' for i in range(40))

        def fake_get(url, **kwargs):
            text = bundle if url.endswith('main.js') else page_html
            return Mock(status_code=200, text=text, content=text.encode())

        with patch.object(flight_search.requests, "get", side_effect=fake_get):
            found = flight_search.discover_endpoints()

        self.assertLessEqual(
            len(found["candidates"]), flight_search.MAX_ENDPOINT_TRIES
        )

    def test_tries_each_candidate_and_reports_which_returned_rows(self):
        def fake_fetch(origin, destination, trip_days, location_type, trip_type,
                       timeout=None, session=None, endpoint=None):
            if endpoint == "https://good.example/graphql":
                return [{"departureDate": "20261010", "minPrice": 1000}], {"status": 200}
            return None, {"status": 400, "error": "GraphQL 오류"}

        with patch.object(flight_search, "fetch_min_prices_by_date", side_effect=fake_fetch), \
             patch.object(flight_search, "warm_session", return_value=Mock()):
            results = flight_search.try_endpoints(
                ["https://bad.example/graphql", "https://good.example/graphql"]
            )

        self.assertEqual(results[0]["rows"], 0)
        self.assertEqual(results[1]["rows"], 1)
        self.assertEqual(results[1]["endpoint"], "https://good.example/graphql")

    def test_endpoint_request_failure_does_not_abort_the_sweep(self):
        def fake_fetch(origin, destination, trip_days, location_type, trip_type,
                       timeout=None, session=None, endpoint=None):
            if endpoint == "https://dead.example/graphql":
                raise flight_search.requests.RequestException("boom")
            return [{"departureDate": "20261010", "minPrice": 1000}], {"status": 200}

        with patch.object(flight_search, "fetch_min_prices_by_date", side_effect=fake_fetch), \
             patch.object(flight_search, "warm_session", return_value=Mock()):
            results = flight_search.try_endpoints(
                ["https://dead.example/graphql", "https://ok.example/graphql"]
            )

        self.assertIn("요청 실패", results[0]["error"])
        self.assertEqual(results[1]["rows"], 1)


class RenderTypeRefTests(unittest.TestCase):
    def test_unwraps_non_null_and_list_wrappers(self):
        ref = _type_ref(
            "NON_NULL",
            of_type=_type_ref("LIST", of_type=_type_ref("INPUT_OBJECT", "Foo")),
        )
        self.assertEqual(flight_search._render_type_ref(ref), "[Foo]!")

    def test_plain_scalar(self):
        self.assertEqual(flight_search._render_type_ref(_type_ref("SCALAR", "Int")), "Int")

    def test_missing_type_is_not_a_crash(self):
        self.assertEqual(flight_search._render_type_ref(None), "?")


class CollectOffersTests(unittest.TestCase):
    CALIBRATION = {"location_type": "CITY", "trip_type": "RT", "sample": []}

    def setUp(self):
        patcher = patch.object(flight_search, "warm_session", return_value=Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_skips_over_cap_fares_and_keeps_destination_metadata(self):
        trips = [(date(2026, 10, 10), date(2026, 10, 12))]
        destinations = (
            {"code": "FUK", "city": "후쿠오카", "country": "일본"},
            {"code": "BKK", "city": "방콕", "country": "태국"},
        )

        def fake_fetch(origin, destination, trip_days, location_type, trip_type, timeout=None, **kwargs):
            price = 150000 if destination == "FUK" else 900000
            return [{"departureDate": "20261010", "returnDate": "20261012", "minPrice": price}], {}

        with patch.object(flight_search, "fetch_min_prices_by_date", side_effect=fake_fetch):
            offers, stats = flight_search.collect_offers(
                destinations=destinations, trips=trips, max_price=400000,
                calibration=self.CALIBRATION,
            )

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["city"], "후쿠오카")
        self.assertEqual(offers[0]["country"], "일본")
        self.assertEqual(offers[0]["price"], 150000)
        self.assertIn("ICN-FUK-20261010", offers[0]["url"])
        # 상한 초과도 '수집 성공'이다 — 실패로 세면 수집 장애와 구분이 안 된다
        self.assertEqual(stats["succeeded"], 2)
        self.assertEqual(stats["failed"], 0)

    def test_gives_up_early_when_nothing_can_be_collected(self):
        """네이버가 막혔을 때 도시 수십 개를 타임아웃마다 기다리지 않는다."""
        trips = [(date(2026, 10, day), date(2026, 10, day + 2)) for day in (3, 10, 17)]
        destinations = tuple(
            {"code": f"C{i}", "city": f"도시{i}", "country": f"나라{i}"} for i in range(20)
        )

        with patch.object(flight_search, "fetch_min_prices_by_date", return_value=(None, {})):
            offers, stats = flight_search.collect_offers(
                destinations=destinations, trips=trips, fail_fast_after=5,
                calibration=self.CALIBRATION,
            )

        self.assertEqual(offers, [])
        self.assertTrue(stats["aborted"])
        self.assertLess(stats["attempted"], stats["total"])

    def test_calibration_failure_aborts_immediately_without_any_requests(self):
        """조합을 하나도 못 찾으면(캘리브레이션 실패) 도시 요청 자체를 안 보낸다."""
        with patch.object(
            flight_search, "calibrate_min_prices_by_date", return_value=(None, []),
        ), patch.object(
            flight_search, "fetch_min_prices_by_date",
        ) as fetch:
            offers, stats = flight_search.collect_offers(
                destinations=({"code": "FUK", "city": "후쿠오카", "country": "일본"},),
                trips=[(date(2026, 10, 10), date(2026, 10, 12))],
            )

        self.assertEqual(offers, [])
        self.assertTrue(stats["aborted"])
        fetch.assert_not_called()


class RunDigestTests(unittest.TestCase):
    def test_sends_the_formatted_digest_and_returns_the_picks(self):
        sent = []
        offers = [_offer(150000, "후쿠오카", "일본"), _offer(900000, "파리", "프랑스")]

        stats = {"attempted": 34, "succeeded": 34, "failed": 0, "total": 34, "aborted": False}
        with patch.object(
            flight_search, "collect_offers", return_value=(offers, stats)
        ), patch.object(
            flight_search, "iter_weekend_trips", return_value=[(date(2026, 10, 10), date(2026, 10, 12))]
        ):
            picked = flight_search.run_weekend_flight_digest(sent.append)

        self.assertEqual([o["city"] for o in picked], ["후쿠오카"])
        self.assertIn("150,000원", sent[0])
        self.assertNotIn("파리", sent[0])


if __name__ == "__main__":
    unittest.main()
