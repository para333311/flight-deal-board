import json
import os
import tempfile
import unittest
from datetime import date, timedelta
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


def _deal(no, title):
    return {
        "title": title,
        "link": f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={no}",
        "date": "2026-07-21",
        "dt_obj": app.parse_date("2026-07-21"),
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

    @patch("app.requests.post")
    def test_send_telegram_message_splits_long_text(self, post):
        post.return_value = Mock(raise_for_status=Mock())
        with patch.object(app, "TELEGRAM_BOT_TOKEN", "token"), patch.object(
            app, "TELEGRAM_CHAT_ID", "12345"
        ):
            self.assertTrue(app.send_telegram_message("가" * 5000))
        self.assertEqual(post.call_count, 2)

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

        # 다음 정기 알림: 모인 게 없으면 조용히 넘어감
        send.reset_mock()
        self.assertEqual(app.flush_deal_digest(), [])
        send.assert_not_called()

        # 대시보드용 캐시 저장 확인
        with open(app.DEALS_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        self.assertEqual(len(cache["deals"]), 2)

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

    @patch("app.requests.get")
    def test_scrape_naver_cafe_uses_open_api(self, get):
        get.return_value = Mock(
            raise_for_status=Mock(),
            json=Mock(return_value={
                "items": [
                    {
                        "title": "<b>제주항공</b> 동남아 특가 &quot;반값&quot;",
                        "link": "https://cafe.naver.com/x/123",
                        "cafename": "스마트컨슈머",
                    },
                    {
                        "title": "제주항공 동남아 특가 중복",
                        "link": "https://cafe.naver.com/x/123",
                        "cafename": "다른카페",
                    },
                ]
            }),
        )
        with patch.object(app, "NAVER_CLIENT_ID", "id"), patch.object(
            app, "NAVER_CLIENT_SECRET", "secret"
        ):
            posts = app.scrape_naver_cafe("네이버카페", "항공권 특가")

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["title"], '[스마트컨슈머] 제주항공 동남아 특가 "반값"')
        self.assertEqual(posts[0]["link"], "https://cafe.naver.com/x/123")

    def test_scrape_naver_cafe_without_keys_returns_empty(self):
        with patch.object(app, "NAVER_CLIENT_ID", ""), patch.object(
            app, "NAVER_CLIENT_SECRET", ""
        ):
            self.assertEqual(app.scrape_naver_cafe("네이버카페", "항공권 특가"), [])

    def test_format_deal_alert_truncates_long_lists(self):
        posts = [_deal(i, f"특가 {i}") for i in range(app.MAX_DEALS_PER_ALERT + 5)]
        message = app.format_deal_alert(posts)
        self.assertIn(f"새 항공 특가 {app.MAX_DEALS_PER_ALERT + 5}건", message)
        self.assertIn("외 5건", message)


if __name__ == "__main__":
    unittest.main()
