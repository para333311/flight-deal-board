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
        self.assertEqual(posts[0]["date"], "2026-01-20")
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


if __name__ == "__main__":
    unittest.main()
