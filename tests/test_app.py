import unittest
from unittest.mock import Mock, patch

import app


NAVER_RESULTS_HTML = """
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/35280361?share=Y">
    남가좌동 227-2번지 일대 신속통합기획 주택재개발사업 후보지 신청 제외 검토
    &gt; 결재문서 &gt; 원문정보 &gt; 정보소통광장
  </a>
  <p>생산일자 : 2026-01-20, 부서명 : 주거정비과</p>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://opengov.seoul.go.kr/sanction/35280361?share=Y">첨부된 문서</a>
</div>
<div class="fds-web-normal-doc-root">
  <a href="https://example.com/not-opengov">재개발 관련 민간 문서 &gt; 결재문서</a>
</div>
"""


class KeywordTests(unittest.TestCase):
    def test_split_keywords_supports_existing_period_format(self):
        self.assertEqual(
            app.split_keywords("재개발.신속통합.일대.후보지"),
            ("재개발", "신속통합", "일대", "후보지"),
        )

    def test_split_keywords_supports_multiple_delimiters_and_deduplicates(self):
        self.assertEqual(
            app.split_keywords("재개발, 신속통합|재개발\n동의서"),
            ("재개발", "신속통합", "동의서"),
        )

    def test_title_matches_any_keyword(self):
        keyword = "재개발.신속통합.정비계획"
        self.assertTrue(app.title_matches_keywords("신속통합기획 후보지 선정", keyword))
        self.assertFalse(app.title_matches_keywords("직원 교육 결과 보고", keyword))
        self.assertTrue(app.title_matches_keywords("모든 제목", ""))


class ScrapeBoardTests(unittest.TestCase):
    @patch("app.requests.Session")
    def test_scraper_uses_or_keyword_matching(self, session_class):
        response = Mock()
        response.text = """
            <table><tbody>
              <tr><td><a href="/sanction/1">신속통합기획 후보지 검토</a></td><td>2026-07-16</td></tr>
              <tr><td><a href="/sanction/2">직원 교육 결과 보고</a></td><td>2026-07-15</td></tr>
            </tbody></table>
        """
        response.apparent_encoding = "utf-8"
        response.raise_for_status = Mock()
        session_class.return_value.get.return_value = response

        posts = app.scrape_board(
            "https://opengov.seoul.go.kr/sanction/list",
            "서울시결재문서",
            "재개발.신속통합.일대.후보지",
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["title"], "신속통합기획 후보지 검토")
        self.assertEqual(posts[0]["link"], "https://opengov.seoul.go.kr/sanction/1")
        self.assertEqual(posts[0]["date"], "2026-07-16")
        response.raise_for_status.assert_called_once_with()


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
        self.assertEqual(posts[0]["date"], "2026-01-20")
        self.assertEqual(session_class.return_value.get.call_count, 2)

    @patch("app.scrape_opengov_search_fallback")
    @patch("app.scrape_board", return_value=[])
    def test_configured_board_uses_fallback_only_for_opengov(
        self, scrape_board, fallback
    ):
        fallback.return_value = [{"title": "복구 문서"}]
        board = {
            "name": "서울시결재문서",
            "url": "https://opengov.seoul.go.kr/sanction/list",
            "keyword": "재개발",
        }

        self.assertEqual(app.scrape_configured_board(board), [{"title": "복구 문서"}])
        fallback.assert_called_once_with("서울시결재문서", "재개발")

        fallback.reset_mock()
        normal_board = {
            "name": "일반 게시판",
            "url": "https://example.com/board",
            "keyword": "재개발",
        }
        self.assertEqual(app.scrape_configured_board(normal_board), [])
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
