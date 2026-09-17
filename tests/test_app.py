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


class BoardScrapingTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
