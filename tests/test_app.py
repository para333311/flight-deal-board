import unittest
from unittest.mock import Mock, patch

import app


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


if __name__ == "__main__":
    unittest.main()
