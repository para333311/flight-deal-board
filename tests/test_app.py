import html
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import app


RECENT_DATE = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")

NAVER_RESULTS_HTML = f"""
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/35280361?share=Y">
    남가좌동 227-2번지 일대 신속통합기획 주택재개발사업 후보지 신청 제외 검토
    &gt; 결재문서 &gt; 원문정보 &gt; 정보소통광장
  </a>
  <p>생산일자 : {RECENT_DATE}, 부서명 : 주거정비과</p>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/35280361?share=Y">첨부된 문서</a>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://example.com/not-opengov">재개발 관련 민간 문서 &gt; 결재문서</a>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/11111111">
    오래된 재개발 문서 &gt; 결재문서 &gt; 정보소통광장
  </a>
  <p>생산일자 : 2025-01-01</p>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/22222222">
    날짜 없는 재개발 문서 &gt; 결재문서 &gt; 정보소통광장
  </a>
</div>
"""

OPEN_PORTAL_ROW = {
    "INFO_SJ": "신속통합기획 주택재개발 후보지 검토",
    "PROC_INSTT_NM": "서울특별시 동작구",
    "NFLST_CHRG_DEPT_NM": "서울특별시 동작구 도시정비과",
    "PRDCTN_INSTT_REGIST_NO": "DCT123",
    "PRDCTN_DT": "20260701093000",
    "INSTT_SE_CD": "B551982",
}


class KeywordTests(unittest.TestCase):
    def test_split_keywords_supports_period_comma_and_deduplication(self):
        self.assertEqual(
            app.split_keywords("재개발.신속통합,재개발|동의서"),
            ("재개발", "신속통합", "동의서"),
        )


class ConfigSourcesTests(unittest.TestCase):
    def test_google_news_query_is_scoped_to_recent_days(self):
        """Google News 검색 RSS는 사실상 '현재 상위 결과' 스냅샷이라, when:
        연산자로 최근 며칠로 좁혀두지 않으면 매번 같은 글만 잡혀 새 글이
        거의 안 나온다. 이 조건이 실수로 빠지지 않도록 잠가둔다."""
        config = json.loads(
            Path(app.__file__).with_name("config.json").read_text(encoding="utf-8")
        )
        board = next(
            b for b in config["deal_boards"] if b["id"] == "google_news_flight_deals"
        )
        self.assertIn("when%3A", board["url"])


class CanonicalizeUrlTests(unittest.TestCase):
    def test_strips_tracking_params_and_sorts_remaining(self):
        url = "https://example.com/deal?no=1&utm_source=fb&utm_medium=share&id=x"
        self.assertEqual(
            app.canonicalize_url(url),
            "https://example.com/deal?id=x&no=1",
        )

    def test_removes_fbclid_and_gclid(self):
        url = "https://example.com/deal?id=1&fbclid=abc&gclid=def"
        self.assertEqual(app.canonicalize_url(url), "https://example.com/deal?id=1")

    def test_two_links_differing_only_by_tracking_params_canonicalize_equal(self):
        a = "https://example.com/deal/123?utm_source=telegram"
        b = "https://example.com/deal/123?utm_campaign=summer"
        self.assertEqual(app.canonicalize_url(a), app.canonicalize_url(b))

    def test_no_query_left_unchanged_besides_host_case(self):
        self.assertEqual(
            app.canonicalize_url("https://EXAMPLE.com/path/"),
            "https://example.com/path",
        )

    def test_empty_or_none_passthrough(self):
        self.assertEqual(app.canonicalize_url(""), "")


class DealScoreTests(unittest.TestCase):
    def test_score_combines_priority_and_bonus_keywords(self):
        board = {"priority": 8}
        post = {"title": "[대한항공] 도쿄 왕복 특가 오픈특가 이벤트"}
        # priority 8 + 왕복/특가/오픈특가 세 단어 매칭
        self.assertEqual(app.compute_deal_score(post, board), 11)

    def test_score_defaults_to_zero_priority_with_no_bonus_words(self):
        self.assertEqual(app.compute_deal_score({"title": "그냥 제목"}, {}), 0)


class TooOldTests(unittest.TestCase):
    def test_recent_post_is_not_too_old(self):
        post = {"dt_obj": datetime.now() - timedelta(days=1)}
        self.assertFalse(app.is_too_old(post, max_age_days=14))

    def test_post_older_than_cutoff_is_too_old(self):
        post = {"dt_obj": datetime.now() - timedelta(days=180)}
        self.assertTrue(app.is_too_old(post, max_age_days=14))

    def test_unknown_date_sentinel_is_never_too_old(self):
        # 공식 이벤트 페이지 등 날짜를 못 구한 글은 걸러낼 근거가 없다.
        post = {"dt_obj": app.UNKNOWN_POST_DATE}
        self.assertFalse(app.is_too_old(post, max_age_days=14))

    def test_missing_dt_obj_is_never_too_old(self):
        self.assertFalse(app.is_too_old({}, max_age_days=14))


class SimplifyGoogleNewsLinkTests(unittest.TestCase):
    """simplify_google_news_link()는 googlenewsdecoder를 먼저 시도하고,
    실패하면 실제 서버 리다이렉트를 따라가는 방식으로 폴백한다. 두 경로
    모두 실제 네트워크를 타므로, 테스트에서는 둘 다 명시적으로 패치해
    이 샌드박스의 네트워크 가용성과 무관하게 결정적으로 동작하게 한다."""

    @patch("googlenewsdecoder.new_decoderv1")
    def test_googlenewsdecoder_success_wins(self, decode):
        decode.return_value = {
            "status": True,
            "decoded_url": "https://news.example.com/article/123?utm_source=rss",
        }
        result = app.simplify_google_news_link(
            "https://news.google.com/rss/articles/CBMi...long...?oc=5"
        )
        self.assertEqual(result, "https://news.example.com/article/123")

    @patch("app.requests.get")
    @patch("googlenewsdecoder.new_decoderv1", side_effect=Exception("decode failed"))
    def test_falls_back_to_redirect_when_decoder_fails(self, decode, get):
        get.return_value = Mock(url="https://news.example.com/article/123?utm_source=rss")
        result = app.simplify_google_news_link(
            "https://news.google.com/rss/articles/CBMi...long...?oc=5"
        )
        self.assertEqual(result, "https://news.example.com/article/123")

    @patch("app.requests.get")
    @patch("googlenewsdecoder.new_decoderv1", side_effect=Exception("decode failed"))
    def test_falls_back_to_original_when_still_on_google(self, decode, get):
        original = "https://news.google.com/rss/articles/CBMi...long...?oc=5"
        get.return_value = Mock(url=original)
        self.assertEqual(app.simplify_google_news_link(original), original)

    @patch("app.requests.get")
    @patch("googlenewsdecoder.new_decoderv1", side_effect=Exception("decode failed"))
    def test_falls_back_to_original_on_request_failure(self, decode, get):
        get.side_effect = app.requests.RequestException("timeout")
        original = "https://news.google.com/rss/articles/CBMi...long...?oc=5"
        self.assertEqual(app.simplify_google_news_link(original), original)


class OpenPortalTests(unittest.TestCase):
    @patch("app.requests.Session")
    def test_official_portal_keeps_recent_seoul_title_matches(self, session_class):
        page_response = Mock()
        page_response.raise_for_status = Mock()
        search_response = Mock()
        search_response.raise_for_status = Mock()
        search_response.json.return_value = {
            "result": {"code": "200", "rtnList": [OPEN_PORTAL_ROW]}
        }
        session_class.return_value.get.return_value = page_response
        session_class.return_value.post.return_value = search_response

        posts = app.scrape_open_portal("서울시결재문서", "신속통합")

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["date"], "2026-07-01")
        self.assertIn("open.go.kr", posts[0]["link"])
        self.assertIn("서울특별시 동작구", posts[0]["title"])


class OpenGovFallbackTests(unittest.TestCase):
    @patch("app.requests.Session")
    def test_fallback_extracts_canonical_document_and_date(self, session_class):
        response = Mock()
        response.text = NAVER_RESULTS_HTML
        response.raise_for_status = Mock()
        session_class.return_value.get.return_value = response

        posts = app.scrape_opengov_search_fallback(
            "서울시결재문서", "재개발.신속통합", limit=15
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0]["link"],
            "https://opengov.seoul.go.kr/sanction/35280361",
        )
        self.assertIn("신속통합기획", posts[0]["title"])
        self.assertEqual(posts[0]["date"], RECENT_DATE)
        self.assertEqual(session_class.return_value.get.call_count, 2)

    @patch("app.scrape_opengov_search_fallback")
    @patch("app.scrape_open_portal", return_value=[])
    @patch("app.scrape_board", return_value=[])
    def test_configured_board_uses_fallback_only_for_opengov(
        self, scrape_board, official_portal, fallback
    ):
        fallback.return_value = [{"title": "복구 문서"}]
        board = {
            "name": "서울시결재문서",
            "url": "https://opengov.seoul.go.kr/sanction/list",
            "keyword": "재개발",
        }

        self.assertEqual(app.scrape_configured_board(board), [{"title": "복구 문서"}])
        official_portal.assert_called_once_with("서울시결재문서", "재개발")
        fallback.assert_called_once_with("서울시결재문서", "재개발")

        official_portal.reset_mock()
        fallback.reset_mock()
        normal_board = {
            "name": "일반 게시판",
            "url": "https://example.com/board",
            "keyword": "재개발",
        }
        self.assertEqual(app.scrape_configured_board(normal_board), [])
        official_portal.assert_not_called()
        fallback.assert_not_called()


DEAL_RECENT_DATE = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _deal(no, title):
    return {
        "title": title,
        "link": f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={no}",
        "date": DEAL_RECENT_DATE,
        "dt_obj": app.parse_date(DEAL_RECENT_DATE),
        "source": "뽐뿌",
    }


class DealNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        sent_file = os.path.join(self.tmpdir.name, "sent_deals.json")
        deals_cache = os.path.join(self.tmpdir.name, "deals_cache.json")
        pending_file = os.path.join(self.tmpdir.name, "pending_deals.json")
        for target, value in (
            ("DATABASE_URL", None),
            ("SENT_DEALS_FILE", sent_file),
            ("DEALS_CACHE_FILE", deals_cache),
            ("PENDING_DEALS_FILE", pending_file),
        ):
            patcher = patch.object(app, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_first_run_seeds_without_notifying(self):
        new_posts, first_run = app.claim_new_deals([_deal(1, "제주항공 특가")])
        self.assertTrue(first_run)
        self.assertEqual(new_posts, [])

        # 두 번째 실행: 기존 글은 무시하고 새 글만 반환
        new_posts, first_run = app.claim_new_deals(
            [_deal(1, "제주항공 특가"), _deal(2, "티웨이 땡처리")]
        )
        self.assertFalse(first_run)
        self.assertEqual([p["title"] for p in new_posts], ["티웨이 땡처리"])

    def test_claim_new_deals_caps_per_run_and_carries_over_the_rest(self):
        """max_new를 넘긴 나머지는 유실이 아니라 다음 회차로 이월된다."""
        # 최초 실행으로 시딩
        app.claim_new_deals([])
        self.assertTrue(os.path.exists(app.SENT_DEALS_FILE))

        posts = [_deal(1, "특가 1"), _deal(2, "특가 2"), _deal(3, "특가 3")]
        first_batch, first_run = app.claim_new_deals(posts, max_new=1)
        self.assertFalse(first_run)
        self.assertEqual([p["title"] for p in first_batch], ["특가 1"])

        # 상한에 걸려 못 들어간 2건은 sent_deals에 기록되지 않았으므로
        # 같은 posts를 다시 넘기면 여전히 새 글로 잡힌다 (유실 아님).
        second_batch, first_run = app.claim_new_deals(posts, max_new=1)
        self.assertFalse(first_run)
        self.assertEqual([p["title"] for p in second_batch], ["특가 2"])

        third_batch, _ = app.claim_new_deals(posts, max_new=10)
        self.assertEqual([p["title"] for p in third_batch], ["특가 3"])

        # 이제 전부 소진됐으니 더 이상 새 글이 없다.
        self.assertEqual(app.claim_new_deals(posts, max_new=10)[0], [])

    @patch("app.requests.post")
    def test_send_telegram_message_splits_long_text(self, post):
        post.return_value = Mock(raise_for_status=Mock())
        with patch.object(app, "TELEGRAM_BOT_TOKEN", "token"), patch.object(
            app, "TELEGRAM_CHAT_ID", "12345"
        ):
            self.assertTrue(app.send_telegram_message("가" * 5000))
        self.assertEqual(post.call_count, 2)

    @patch("app.requests.post")
    def test_send_telegram_message_uses_html_parse_mode(self, post):
        post.return_value = Mock(raise_for_status=Mock())
        with patch.object(app, "TELEGRAM_BOT_TOKEN", "token"), patch.object(
            app, "TELEGRAM_CHAT_ID", "12345"
        ):
            app.send_telegram_message("안녕")
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")

    def test_send_telegram_message_requires_configuration(self):
        with patch.object(app, "TELEGRAM_BOT_TOKEN", ""), patch.object(
            app, "TELEGRAM_CHAT_ID", ""
        ):
            self.assertFalse(app.send_telegram_message("테스트"))

    @patch("app.send_telegram_message", return_value=True)
    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_queues_and_digest_sends(
        self, load_config, scrape, send
    ):
        load_config.return_value = {
            "deal_boards": [{"name": "뽐뿌", "url": "https://example.com", "keyword": "항공"}]
        }

        # 최초 실행: 기존 글은 알림 없이 기록만 (재시작 시 인사 반복 방지)
        scrape.return_value = [_deal(1, "제주항공 동남아 50% 할인코드")]
        self.assertEqual(app.check_airline_deals(), [])
        send.assert_not_called()

        # 새 글 등장: 즉시 보내지 않고 대기 목록에 쌓임
        scrape.return_value = [
            _deal(1, "제주항공 동남아 50% 할인코드"),
            _deal(2, "티웨이 국제선 특가 오픈"),
        ]
        new_posts = app.check_airline_deals()
        self.assertEqual(len(new_posts), 1)
        send.assert_not_called()

        # 정기 알림 시각: 모인 특가를 묶어서 1회 전송 후 목록 비움
        sent = app.flush_deal_digest()
        self.assertEqual(len(sent), 1)
        message = send.call_args[0][0]
        self.assertIn("항공 특가 모음 1건", message)
        self.assertIn("티웨이 국제선 특가 오픈", message)
        self.assertIn("no=2", message)
        self.assertIn('<a href="https://www.ppomppu.co.kr', message)

        # 다음 정기 알림: 모인 게 없으면 조용히 넘어감
        send.reset_mock()
        self.assertEqual(app.flush_deal_digest(), [])
        send.assert_not_called()

        # 대시보드용 캐시 저장 확인
        with open(app.DEALS_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        self.assertEqual(len(cache["deals"]), 2)

    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_skips_disabled_boards(self, load_config, scrape):
        load_config.return_value = {
            "deal_boards": [
                {"name": "켜짐", "url": "https://a.example.com", "keyword": "항공", "enabled": True},
                {"name": "꺼짐", "url": "https://b.example.com", "keyword": "항공", "enabled": False},
            ]
        }
        scrape.return_value = [_deal(1, "제주항공 특가")]

        app.check_airline_deals()

        called_boards = [call.args[0]["name"] for call in scrape.call_args_list]
        self.assertIn("켜짐", called_boards)
        self.assertNotIn("꺼짐", called_boards)

    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_applies_global_include_keyword_when_board_has_none(
        self, load_config, scrape
    ):
        load_config.return_value = {
            "deal_include_keyword": "항공권.특가",
            "deal_boards": [{"name": "공식이벤트", "url": "https://airline.example.com", "keyword": ""}],
        }
        scrape.return_value = []

        app.check_airline_deals()

        used_board = scrape.call_args[0][0]
        self.assertEqual(used_board["keyword"], "항공권.특가")

    @patch("app.send_telegram_message", return_value=True)
    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_one_source_failure_does_not_break_others(
        self, load_config, scrape, send
    ):
        """소스 하나가 예외를 던져도 나머지 소스는 정상 수집된다."""
        load_config.return_value = {
            "deal_boards": [
                {"name": "고장남", "url": "https://broken.example.com", "keyword": "항공"},
                {"name": "정상", "url": "https://ok.example.com", "keyword": "항공"},
            ]
        }

        def side_effect(board):
            if board["name"] == "고장남":
                raise RuntimeError("접속 실패")
            return [_deal(1, "제주항공 특가")]

        scrape.side_effect = side_effect

        # 최초 실행(시딩)은 알림이 없으므로, 시딩만 미리 해두고 본 실행에서 확인한다.
        app.claim_new_deals([])
        new_posts = app.check_airline_deals()

        self.assertEqual([p["title"] for p in new_posts], ["제주항공 특가"])

    @patch("app.send_telegram_message", return_value=True)
    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_caps_new_alerts_per_run(self, load_config, scrape, send):
        load_config.return_value = {
            "deal_boards": [{"name": "뽐뿌", "url": "https://example.com", "keyword": "항공"}]
        }
        with patch.object(app, "DEAL_MAX_ALERTS_PER_RUN", 2):
            scrape.return_value = []
            self.assertEqual(app.check_airline_deals(), [])  # 최초 실행 시딩

            scrape.return_value = [_deal(i, f"특가 {i}") for i in range(1, 6)]
            new_posts = app.check_airline_deals()

        self.assertEqual(len(new_posts), 2)

    @patch("app.scrape_configured_board")
    @patch("app.load_config")
    def test_check_airline_deals_excludes_stale_news(self, load_config, scrape):
        """Google News가 재색인한 몇 달 전 기사는 새 글로 잡히지 않는다."""
        load_config.return_value = {
            "deal_boards": [{"name": "GoogleNews", "url": "https://news.google.com/rss", "keyword": ""}]
        }
        old = _deal(1, "6개월 전 항공권 특가 기사")
        old["dt_obj"] = datetime.now() - timedelta(days=200)
        fresh = _deal(2, "오늘자 항공권 특가 기사")

        scrape.return_value = []
        self.assertEqual(app.check_airline_deals(), [])  # 최초 실행 시딩

        scrape.return_value = [old, fresh]
        new_posts = app.check_airline_deals()

        self.assertEqual([p["title"] for p in new_posts], ["오늘자 항공권 특가 기사"])

    @patch("app.simplify_google_news_link")
    def test_add_pending_deals_simplifies_only_google_news_links(self, simplify):
        simplify.return_value = "https://news.example.com/simplified"
        google_deal = _deal(1, "구글뉴스 특가")
        google_deal["link"] = "https://news.google.com/rss/articles/abc?oc=5"
        other_deal = _deal(2, "뽐뿌 특가")

        app.add_pending_deals([google_deal, other_deal])

        simplify.assert_called_once_with("https://news.google.com/rss/articles/abc?oc=5")
        with open(app.PENDING_DEALS_FILE, encoding="utf-8") as f:
            pending = json.load(f)
        links = {p["title"]: p["link"] for p in pending}
        self.assertEqual(links["구글뉴스 특가"], "https://news.example.com/simplified")
        self.assertEqual(links["뽐뿌 특가"], other_deal["link"])

    @patch("app.send_telegram_message", return_value=True)
    @patch("app.simplify_google_news_link", return_value="https://news.example.com/decoded")
    def test_flush_digest_removes_pending_with_original_link_even_after_display_simplify(
        self, simplify, send
    ):
        """전송용 링크를 단순화해도 대기 목록 삭제는 원본 링크 기준으로 동작해야 한다."""
        google = _deal(1, "구글뉴스 항공 특가")
        google.pop("dt_obj", None)
        google["link"] = "https://news.google.com/rss/articles/abc?oc=5"
        with open(app.PENDING_DEALS_FILE, "w", encoding="utf-8") as f:
            json.dump([google], f, ensure_ascii=False)

        sent = app.flush_deal_digest()

        self.assertEqual(len(sent), 1)
        simplify.assert_called_once_with("https://news.google.com/rss/articles/abc?oc=5")
        self.assertIn("https://news.example.com/decoded", send.call_args[0][0])

        with open(app.PENDING_DEALS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

        send.reset_mock()
        self.assertEqual(app.flush_deal_digest(), [])
        send.assert_not_called()

    @patch("app.send_telegram_message", return_value=True)
    def test_digest_drops_already_queued_excluded_deals(self, send):
        """필터를 넓히기 전에 대기 목록에 쌓인 휴대폰 글은 전송 전에 버린다."""
        phone = _deal(1, "[성지모아 - 휴대폰 성지 시세표] 플립8 폴드8 사전예약 시작")
        flight = _deal(2, "[매직뱅크] 2026년 7월 27일 월, 여행 항공")
        for deal in (phone, flight):
            deal.pop("dt_obj", None)
        with open(app.PENDING_DEALS_FILE, "w", encoding="utf-8") as f:
            json.dump([phone, flight], f, ensure_ascii=False)

        sent = app.flush_deal_digest()

        self.assertEqual([p["title"] for p in sent], [flight["title"]])
        self.assertNotIn("휴대폰", send.call_args[0][0])
        # 걸러진 글은 대기 목록에서도 지워져 다음 회차에 다시 나오지 않는다
        with open(app.PENDING_DEALS_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])
        send.reset_mock()
        self.assertEqual(app.flush_deal_digest(), [])
        send.assert_not_called()

    @patch("app.send_telegram_message")
    @patch("app.scrape_configured_board", return_value=[])
    @patch("app.load_config")
    def test_check_airline_deals_is_silent_when_no_posts(
        self, load_config, scrape, send
    ):
        load_config.return_value = {
            "deal_boards": [{"name": "뽐뿌", "url": "https://example.com", "keyword": "항공"}]
        }
        # 최초 실행이든 이후든, 글이 없으면 아무 알림도 보내지 않는다
        self.assertEqual(app.check_airline_deals(), [])
        self.assertEqual(app.check_airline_deals(), [])
        send.assert_not_called()

    @patch("app.requests.get")
    def test_scrape_rss_filters_by_keyword(self, get):
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>[제주항공] 동남아 최대 50% 할인코드 (수수료무료)</title>
            <link>https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=101</link>
            <pubDate>Tue, 21 Jul 2026 09:00:00 +0900</pubDate>
          </item>
          <item>
            <title>노트북 특가</title>
            <link>https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&amp;no=102</link>
            <pubDate>Tue, 21 Jul 2026 08:00:00 +0900</pubDate>
          </item>
        </channel></rss>"""
        get.return_value = Mock(
            content=rss.encode("utf-8"), raise_for_status=Mock()
        )

        posts = app.scrape_rss("https://example.com/rss.php?id=ppomppu", "뽐뿌RSS", "항공.티웨이")

        self.assertEqual(len(posts), 1)
        self.assertIn("제주항공", posts[0]["title"])
        self.assertEqual(posts[0]["date"], "2026-07-21")
        self.assertIn("no=101", posts[0]["link"])

    def test_configured_board_routes_rss_type(self):
        with patch("app.scrape_rss", return_value=[{"title": "x"}]) as rss:
            board = {"name": "뽐뿌RSS", "url": "https://a.b/rss.php?id=ppomppu", "keyword": "항공"}
            self.assertEqual(app.scrape_configured_board(board), [{"title": "x"}])
            rss.assert_called_once()

    def test_configured_board_excludes_by_keyword(self):
        collected = [
            {"title": "부산출발 세부 5일 특가", "link": "a"},
            {"title": "인천출발 다낭 항공권 특가", "link": "b"},
            {"title": "김포 출발 오사카", "link": "c"},
            {"title": "(무자본)개인 사업 부업 하실분", "link": "d"},
        ]
        with patch("app._collect_board_posts", return_value=collected):
            board = {
                "name": "t",
                "url": "https://a.b/rss.php",
                "keyword": "",
                "exclude_keyword": "부산출발.부업",
            }
            titles = [p["title"] for p in app.scrape_configured_board(board)]

        self.assertIn("인천출발 다낭 항공권 특가", titles)
        self.assertIn("김포 출발 오사카", titles)
        self.assertNotIn("부산출발 세부 5일 특가", titles)
        self.assertNotIn("(무자본)개인 사업 부업 하실분", titles)

    def test_configured_board_excludes_mobile_phone_posts(self):
        """휴대폰 성지/시세표 글을 걸러낸다."""
        exclude = json.loads(
            Path(app.__file__).with_name("config.json").read_text(encoding="utf-8")
        )["deal_exclude_keyword"]
        collected = [
            {"title": "[세모지 휴대폰성지 시세표 좌표 내방 S26 아이폰18 Z플립8 폴드8] 폴더블8 사전예약!", "link": "a"},
            {"title": "[휴대폰 성지 동네빠삭 - 핸드폰성지,시세표,휴대폰싸게사는법] 부천 시세표입니다.", "link": "b"},
            {"title": "[웨딩 아카이브] 신혼여행 항공권, 언제 끊어야 진짜 저렴할까요?", "link": "c"},
            {"title": "[매직뱅크] 2026년 7월 27일 월, 여행 항공", "link": "d"},
        ]
        with patch("app._collect_board_posts", return_value=collected):
            board = {"name": "t", "url": "https://a.b", "keyword": "", "exclude_keyword": exclude}
            titles = [p["title"] for p in app.scrape_configured_board(board)]

        self.assertEqual(len(titles), 2)
        self.assertTrue(all("휴대폰" not in t and "시세표" not in t for t in titles))
        self.assertIn("[매직뱅크] 2026년 7월 27일 월, 여행 항공", titles)

    def test_configured_board_excludes_portal_syndicated_duplicates(self):
        """Google News가 물어오는 v.daum.net/네이트 재배포 중복 기사를 뺀다."""
        exclude = json.loads(
            Path(app.__file__).with_name("config.json").read_text(encoding="utf-8")
        )["deal_exclude_keyword"]
        collected = [
            {"title": "에어부산, 동계 국내선 항공권 특가... 편도 2만3900원부터 - v.daum.net", "link": "a"},
            {"title": "에어부산, 동계 국내선 항공권 특가... 편도 2만3900원부터 - 네이트", "link": "b"},
            {"title": "추석 연휴·가을 항공권 특가 판매...11일 오전 예매 노리자 - gukjenews.com", "link": "c"},
        ]
        with patch("app._collect_board_posts", return_value=collected):
            board = {"name": "t", "url": "https://a.b", "keyword": "", "exclude_keyword": exclude}
            titles = [p["title"] for p in app.scrape_configured_board(board)]

        self.assertEqual(titles, ["추석 연휴·가을 항공권 특가 판매...11일 오전 예매 노리자 - gukjenews.com"])

    @patch("app.requests.Session")
    def test_scrape_board_parses_clien_style_list(self, session_class):
        response = Mock()
        response.text = """
        <div class="contents_jirum">
          <div class="list_item symph_row">
            <a class="list_reply" href="#comment">5</a>
            <a class="list_subject" href="/service/board/jirum/1234">
              <span class="subject_fixed">티웨이항공 국제선 특가 오픈</span>
            </a>
            <span class="timestamp">2026-07-21 09:00</span>
          </div>
          <div class="list_item symph_row">
            <a class="list_subject" href="/service/board/jirum/1235">
              <span class="subject_fixed">노트북 할인</span>
            </a>
            <span class="timestamp">2026-07-21 08:00</span>
          </div>
        </div>
        """
        response.encoding = "utf-8"
        session_class.return_value.get.return_value = response

        posts = app.scrape_board("https://www.clien.net/service/board/jirum", "클리앙", "항공")

        self.assertEqual(len(posts), 1)
        self.assertIn("티웨이항공", posts[0]["title"])
        self.assertIn("/service/board/jirum/1234", posts[0]["link"])

    @patch("app.requests.Session")
    def test_scrape_board_falls_back_to_all_anchor_tags(self, session_class):
        """알려진 목록/테이블 구조가 없는 페이지(공식 이벤트 페이지 등)에서는
        페이지의 모든 <a> 태그를 훑어 키워드로 매칭한다."""
        response = Mock()
        response.text = """
        <nav><a href="/">홈</a></nav>
        <section class="promo-cards">
          <a href="/event/1">[특가] 도쿄 왕복 항공권 할인 이벤트</a>
          <a href="/event/2">회사 소개</a>
        </section>
        """
        response.encoding = "utf-8"
        session_class.return_value.get.return_value = response

        posts = app.scrape_board("https://airline.example.com/event/list", "테스트항공", "항공")

        self.assertEqual(len(posts), 1)
        self.assertIn("도쿄 왕복", posts[0]["title"])
        self.assertEqual(posts[0]["link"], "https://airline.example.com/event/1")
        self.assertEqual(posts[0]["date"], "")

    @patch("app.requests.Session")
    def test_scrape_board_canonicalizes_link_dropping_utm_params(self, session_class):
        response = Mock()
        response.text = """
        <div class="list_item">
          <a class="list_subject" href="/view?no=1&utm_source=fb">항공 특가 링크</a>
        </div>
        """
        response.encoding = "utf-8"
        session_class.return_value.get.return_value = response

        posts = app.scrape_board("https://example.com/board", "테스트", "항공")

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["link"], "https://example.com/view?no=1")

    def test_format_deal_alert_truncates_long_lists(self):
        posts = [_deal(i, f"특가 {i}") for i in range(app.MAX_DEALS_PER_ALERT + 5)]
        message = app.format_deal_alert(posts)
        self.assertIn(f"새 항공 특가 {app.MAX_DEALS_PER_ALERT + 5}건", message)
        self.assertIn("외 5건", message)

    def test_format_deal_alert_hides_long_link_behind_short_anchor_text(self):
        """긴 링크를 그대로 노출하지 않고 '링크'라는 짧은 글자에 매단다."""
        post = _deal(1, "특가")
        post["link"] = (
            "https://news.google.com/rss/articles/"
            "CBMiWEFVX3lxTFBqUmRmWtkSjhBc194WEhqaTU5T05ld1pjUEs5dVdOelF?oc=5"
        )
        message = app.format_deal_alert([post])

        self.assertIn(f'<a href="{post["link"]}">링크</a>', message)
        # 사람이 읽는 줄에는 원본 URL 그대로가 노출되지 않는다 (href 속성 안에만 존재)
        visible_lines = [line for line in message.split("\n") if not line.startswith('🔗 <a href="')]
        self.assertFalse(any(post["link"] in line for line in visible_lines))

    def test_format_deal_alert_escapes_html_special_chars_in_title(self):
        post = _deal(1, '특가 <5&6> "할인"')
        message = app.format_deal_alert([post])
        self.assertIn(html.escape('특가 <5&6> "할인"'), message)
        self.assertNotIn('특가 <5&6> "할인"', message)  # 이스케이프 안 된 원문은 없어야 함

    def test_format_deal_alert_omits_source_and_matched_lines(self):
        """출처/매칭 줄은 메시지에서 뺀다 (사용자 요청)."""
        message = app.format_deal_alert([_deal(1, "특가")])
        self.assertNotIn("출처:", message)
        self.assertNotIn("매칭:", message)

    def test_format_deal_alert_max_shown_none_includes_everything(self):
        posts = [_deal(i, f"특가 {i}") for i in range(app.MAX_DEALS_PER_ALERT + 51)]
        message = app.format_deal_alert(posts, max_shown=None)
        self.assertNotIn("외 ", message)
        for post in posts:
            self.assertIn(post["title"], message)
            self.assertIn(html.escape(post["link"], quote=True), message)

    def test_split_message_keeps_links_intact(self):
        # 4096자를 훌쩍 넘는 메시지를 만들어 링크가 중간에 끊기지 않는지 확인
        posts = [_deal(i, f"아주 긴 항공 특가 제목 테스트 {i} " + "가나다" * 20) for i in range(80)]
        message = app.format_deal_alert(posts, max_shown=None)
        chunks = app.split_message_for_telegram(message)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), app.TELEGRAM_MESSAGE_LIMIT)

        # 모든 링크가 어느 한 조각 안에 온전히 존재해야 함 (HTML 이스케이프된 형태로)
        for post in posts:
            escaped_link = html.escape(post["link"], quote=True)
            self.assertTrue(
                any(escaped_link in chunk for chunk in chunks),
                f"링크가 조각 사이에서 끊김: {post['link']}",
            )

        # 조각을 다시 합치면 원본과 동일 (줄 단위 분할이므로 개행으로 이어 붙임)
        self.assertEqual("\n".join(chunks), message)

    def test_split_message_short_text_single_chunk(self):
        self.assertEqual(app.split_message_for_telegram("안녕"), ["안녕"])

    @patch("app.requests.post")
    def test_flush_deal_digest_sends_all_pending_without_truncation(self, post):
        post.return_value.raise_for_status = lambda: None
        pending = []
        for i in range(app.MAX_DEALS_PER_ALERT + 51):
            deal = _deal(i, f"특가 {i}")
            deal.pop("dt_obj", None)
            pending.append(deal)
        with open(app.PENDING_DEALS_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False)

        with patch.object(app, "TELEGRAM_BOT_TOKEN", "t"), patch.object(
            app, "TELEGRAM_CHAT_ID", "c"
        ):
            sent = app.flush_deal_digest()

        self.assertEqual(len(sent), len(pending))
        combined = "".join(
            call.kwargs["json"]["text"] for call in post.call_args_list
        )
        self.assertNotIn("외 ", combined)
        for deal in pending:
            self.assertIn(html.escape(deal["link"], quote=True), combined)


if __name__ == "__main__":
    unittest.main()
