import json
import os
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urljoin
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import pytz
import psycopg2
from psycopg2.extras import RealDictCursor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

CONFIG_FILE = 'config.json'
CACHE_FILE = 'cache.json'
VISITORS_FILE = 'visitors.json'
SENT_DEALS_FILE = 'sent_deals.json'
DEALS_CACHE_FILE = 'deals_cache.json'
ADMIN_PASSWORD = "1111" # 기본 비밀번호
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
NAVER_CLIENT_ID = os.environ.get('NAVER_CLIENT_ID', '')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET', '')
NAVER_CAFE_API_URL = 'https://openapi.naver.com/v1/search/cafearticle.json'
TELEGRAM_MESSAGE_LIMIT = 4096
DEAL_CHECK_INTERVAL_MINUTES = 30
MAX_DEALS_PER_ALERT = 10
DATABASE_URL = os.environ.get('DATABASE_URL')  # Render에서 자동으로 제공
OPENGOV_HOST = 'opengov.seoul.go.kr'
OPEN_PORTAL_LIST_URL = 'https://www.open.go.kr/othicInfo/infoList/infoList.do'
OPEN_PORTAL_SEARCH_URL = 'https://www.open.go.kr/othicInfo/infoList/mnstrSanDocList.ajax'
NAVER_WEB_SEARCH_URL = 'https://search.naver.com/search.naver'
OPENGOV_DOC_ID_RE = re.compile(r'opengov\.seoul\.go\.kr/sanction/(\d+)')
RECENT_DOCUMENT_DAYS = 180
POSTS_PER_BOARD = 5
SEOUL_TARGET_DISTRICTS = (
    '강남구', '강동구', '광진구', '동대문구', '동작구', '마포구', '서대문구',
    '서초구', '성동구', '송파구', '영등포구', '용산구', '종로구', '중구',
)

# 스케줄러 초기화
scheduler = BackgroundScheduler()
scheduler.start()

# 앱 종료 시 스케줄러도 종료
atexit.register(lambda: scheduler.shutdown())

def get_korean_time():
    """한국 시간(KST) 반환"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)

def get_db_connection():
    """PostgreSQL 연결"""
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return None

def init_db():
    """방문자 테이블 생성 (없을 경우)"""
    if not DATABASE_URL:
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS visitors (
                id SERIAL PRIMARY KEY,
                date DATE UNIQUE NOT NULL,
                today_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sent_deals (
                link TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                sent_at TIMESTAMP DEFAULT NOW()
            )
        """)

        conn.commit()
        cursor.close()
        conn.close()
        print("✅ PostgreSQL 테이블 초기화 완료")
    except Exception as e:
        print(f"❌ DB 초기화 오류: {e}")

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"boards": []}

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def get_headers(url):
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': url
    }

def parse_date(date_str):
    if not date_str: return datetime(1900, 1, 1)
    try:
        clean_date = re.sub(r'[^0-9-]', '-', date_str.replace('.', '-')).strip('-')
        parts = clean_date.split('-')
        if len(parts[0]) == 2: parts[0] = '20' + parts[0]
        return datetime.strptime("-".join(parts[:3]), '%Y-%m-%d')
    except:
        return datetime(1900, 1, 1)

def split_keywords(keyword):
    """마침표나 쉼표로 구분된 검색어를 OR 검색어 목록으로 변환한다."""
    if not keyword:
        return ()
    return tuple(
        dict.fromkeys(
            term.strip()
            for term in re.split(r'[.,|·\n]+', keyword)
            if term.strip()
        )
    )


def scrape_board(url, name, keyword):
    posts = []
    try:
        session = requests.Session()
        response = session.get(url, headers=get_headers(url), verify=False, timeout=15)
        # 서버가 charset을 명시하면 그대로 쓴다 (뽐뿌 등 EUC-KR 사이트 한글 깨짐 방지).
        # 명시가 없으면 requests 기본값(ISO-8859-1) 대신 내용 기반 추정을 사용한다.
        if not response.encoding or response.encoding.lower() == 'iso-8859-1':
            response.encoding = response.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        rows = soup.select('table tbody tr, .board-list tr, .bbs-list tr, .list_type li, .news-list li, .search-result-list li, .list-wrap li, .list_item')
        if not rows:
            rows = soup.select('.title, .subject, .txt_left, .tit')

        for row in rows:
            # 뽐뿌/클리앙/루리웹처럼 제목 앞에 분류·댓글 링크가 붙는 게시판은
            # 제목 앵커를 우선 사용
            title_elem = (
                row.select_one('a.baseList-title, a.list_subject, a.deco')
                or row.select_one('a, .tit, .subject, .title')
            )
            if not title_elem: continue

            title = title_elem.get_text(strip=True)
            if len(title) < 3: continue

            # 키워드 필터링 (여러 키워드 지원: OR 조건)
            keywords = split_keywords(keyword)
            if keywords and not any(kw in title for kw in keywords):
                continue

            link = title_elem.get('href', '')
            if not link or '#' in link or 'javascript' in link:
                parent_a = row.find_parent('a') or row.find('a')
                if parent_a: link = parent_a.get('href', '')

            full_link = urljoin(url, link)

            date_val = ""
            for elem in row.select('td, span, time, .date, .reg_date, .day'):
                txt = elem.get_text(strip=True)
                if re.search(r'\d{2,4}[-./]\d{1,2}[-./]\d{1,2}', txt):
                    date_val = txt
                    break

            posts.append({
                'title': title,
                'link': full_link,
                'date': date_val,
                'dt_obj': parse_date(date_val),
                'source': name
            })

        posts.sort(key=lambda x: x['dt_obj'], reverse=True)

    except Exception as e:
        print(f"Error scraping {name}: {e}")

    return posts


def scrape_open_portal(name, keyword, limit=30):
    """정보공개포털 공식 AJAX 검색에서 최근 서울시 결재문서를 가져온다."""
    keywords = split_keywords(keyword)
    if not keywords:
        return []

    today = date.today()
    start = today - timedelta(days=RECENT_DOCUMENT_DAYS)
    session = requests.Session()
    session.get(
        OPEN_PORTAL_LIST_URL,
        headers=get_headers(OPEN_PORTAL_LIST_URL),
        timeout=20,
    ).raise_for_status()

    posts_by_id = {}
    headers = get_headers(OPEN_PORTAL_LIST_URL)
    headers['X-Requested-With'] = 'XMLHttpRequest'
    for term in keywords:
        payload = {
            'kwd': term,
            'preKwds': term,
            'reSrchFlag': 'off',
            'othbcSeCd': '',
            'insttSeCd': '',
            'eduYn': 'N',
            'startDate': start.strftime('%Y%m%d'),
            'endDate': today.strftime('%Y%m%d'),
            'insttCdNm': '',
            'insttCd': '',
            'searchInsttCdNmPop': '',
            'searchMainYn': '',
            'viewPage': '1',
            'rowPage': '100',
            'sort': 's',
        }
        response = session.post(
            OPEN_PORTAL_SEARCH_URL,
            data=payload,
            headers=headers,
            timeout=50,
        )
        response.raise_for_status()
        data = response.json()
        result = data.get('result') or data
        if str(result.get('code')) != '200':
            raise RuntimeError(
                f"Open.go.kr search error ({term}): {result.get('code')}"
            )

        for row in result.get('rtnList') or []:
            title = str(row.get('INFO_SJ') or '').strip()
            if not title or term not in title:
                continue

            agency = str(row.get('PROC_INSTT_NM') or '').strip()
            department = str(row.get('NFLST_CHRG_DEPT_NM') or '').strip()
            is_seoul = agency == '서울특별시' or any(
                agency == f'서울특별시 {district}'
                or f'서울특별시 {district}' in department
                for district in SEOUL_TARGET_DISTRICTS
            )
            if not is_seoul:
                continue

            document_id = str(row.get('PRDCTN_INSTT_REGIST_NO') or '').strip()
            produced = str(row.get('PRDCTN_DT') or '').strip()
            if not document_id or len(produced) < 8 or not produced[:8].isdigit():
                continue

            date_value = f'{produced[:4]}-{produced[4:6]}-{produced[6:8]}'
            query = urlencode({
                'prdnNstRgstNo': document_id,
                'prdnDt': produced,
                'nstSeCd': str(row.get('INSTT_SE_CD') or '').strip(),
                'title': '기관장결재문서',
            })
            posts_by_id[document_id] = {
                'title': f'[{agency or "서울특별시"}] {title}',
                'link': f'https://www.open.go.kr/othicInfo/infoList/infoListDetl3.do?{query}',
                'date': date_value,
                'dt_obj': parse_date(date_value),
                'source': name,
            }

    posts = sorted(
        posts_by_id.values(), key=lambda post: post['dt_obj'], reverse=True
    )
    app.logger.info('%s: Open.go.kr recovered %d documents', name, len(posts))
    return posts[:limit]


def scrape_opengov_search_fallback(name, keyword, limit=30):
    """정보소통광장 접속 차단 시 네이버 웹문서 색인에서 결과를 복구한다.

    Render 같은 해외 서버에서는 정보소통광장 TCP 연결이 자주 차단된다.
    네이버 검색 결과 중 숫자형 결재문서 URL과 제목만 추출하여 빈 카드가
    계속 노출되는 것을 막는다. 상세문서는 원래 정보소통광장으로 연결한다.
    """
    keywords = split_keywords(keyword)
    if not keywords:
        return []

    posts_by_id = {}
    session = requests.Session()
    for term in keywords:
        try:
            response = session.get(
                NAVER_WEB_SEARCH_URL,
                params={
                    'where': 'web',
                    'query': f'site:{OPENGOV_HOST}/sanction {term}',
                },
                headers=get_headers(NAVER_WEB_SEARCH_URL),
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            app.logger.warning('OpenGov fallback search failed for %s: %s', term, exc)
            continue

        soup = BeautifulSoup(response.text, 'html.parser')
        for anchor in soup.find_all('a', href=True):
            document_ids = OPENGOV_DOC_ID_RE.findall(anchor.get('href', ''))
            raw_title = ' '.join(anchor.get_text(' ', strip=True).split())
            if not document_ids or '> 결재문서' not in raw_title:
                continue

            title = raw_title.split('> 결재문서', 1)[0].strip(' .')
            if len(title) < 3 or term not in title:
                continue

            document_id = document_ids[-1]
            if document_id in posts_by_id:
                continue

            container = anchor.find_parent(
                'div', class_=lambda classes: classes and 'fds-web-normal-doc-root' in classes
            )
            container_text = container.get_text(' ', strip=True) if container else ''
            date_match = re.search(r'20\d{2}[-.]\d{1,2}[-.]\d{1,2}', container_text)
            if not date_match:
                continue
            date_value = date_match.group()
            date_object = parse_date(date_value)
            cutoff = datetime.combine(
                date.today() - timedelta(days=RECENT_DOCUMENT_DAYS), datetime.min.time()
            )
            if date_object < cutoff:
                continue
            posts_by_id[document_id] = {
                'title': title,
                'link': f'https://{OPENGOV_HOST}/sanction/{document_id}',
                'date': date_value,
                'dt_obj': date_object,
                'source': name,
            }

            if len(posts_by_id) >= limit:
                break
        if len(posts_by_id) >= limit:
            break

    posts = list(posts_by_id.values())
    posts.sort(key=lambda post: post['dt_obj'], reverse=True)
    app.logger.info('%s: search fallback recovered %d OpenGov documents', name, len(posts))
    return posts


def scrape_rss(url, name, keyword):
    """RSS 피드에서 글 목록을 가져온다. (뽐뿌 등 HTML 차단 시 우회 경로)"""
    posts = []
    try:
        response = requests.get(url, headers=get_headers(url), verify=False, timeout=15)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        keywords = split_keywords(keyword)
        for item in root.iter('item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            if len(title) < 3 or not link:
                continue
            if keywords and not any(kw in title for kw in keywords):
                continue

            pub_date = (item.findtext('pubDate') or '').strip()
            date_val = ''
            dt_obj = datetime(1900, 1, 1)
            if pub_date:
                try:
                    dt_obj = parsedate_to_datetime(pub_date).replace(tzinfo=None)
                    date_val = dt_obj.strftime('%Y-%m-%d')
                except (TypeError, ValueError):
                    dt_obj = parse_date(pub_date)
                    date_val = pub_date

            posts.append({
                'title': title,
                'link': link,
                'date': date_val,
                'dt_obj': dt_obj,
                'source': name,
            })

        posts.sort(key=lambda x: x['dt_obj'], reverse=True)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"Error scraping RSS {name}: {e}")
    return posts


def scrape_naver_cafe(name, keyword):
    """네이버 검색 오픈API로 공개 카페글을 검색한다.

    네이버 카페는 로그인 장벽 때문에 직접 크롤링이 불가능하므로 공식 검색
    API(cafearticle)를 사용한다. NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
    환경변수가 있어야 동작하며, 없으면 빈 목록을 반환한다.
    키워드 각각을 검색어로 사용한다. (예: "항공권 특가.항공 땡처리")
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []

    keywords = split_keywords(keyword)
    if not keywords:
        return []

    tag_re = re.compile(r'<[^>]+>')
    posts_by_link = {}
    headers = {
        'X-Naver-Client-Id': NAVER_CLIENT_ID,
        'X-Naver-Client-Secret': NAVER_CLIENT_SECRET,
    }
    for term in keywords:
        try:
            response = requests.get(
                NAVER_CAFE_API_URL,
                params={'query': term, 'display': 30, 'sort': 'date'},
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            app.logger.warning('네이버 카페 검색 실패 (%s): %s', term, exc)
            continue

        for item in response.json().get('items', []):
            title = tag_re.sub('', item.get('title') or '')
            title = title.replace('&quot;', '"').replace('&amp;', '&').strip()
            link = (item.get('link') or '').strip()
            if len(title) < 3 or not link or link in posts_by_link:
                continue
            cafe = (item.get('cafename') or '').strip()
            posts_by_link[link] = {
                'title': f'[{cafe}] {title}' if cafe else title,
                'link': link,
                'date': '',
                # API가 작성일을 주지 않으므로 최신순 정렬만 신뢰한다
                'dt_obj': datetime(1900, 1, 1),
                'source': name,
            }

    return list(posts_by_link.values())


def scrape_configured_board(board):
    """게시판을 수집하고 정보소통광장은 공식 포털을 우선 사용한다."""
    keyword = board.get('keyword', '')
    if board.get('type') == 'naver_cafe':
        return scrape_naver_cafe(board['name'], keyword)
    if board.get('type') == 'rss' or 'rss.php' in board.get('url', ''):
        return scrape_rss(board['url'], board['name'], keyword)
    if OPENGOV_HOST in board.get('url', ''):
        try:
            posts = scrape_open_portal(board['name'], keyword)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            app.logger.warning('Open.go.kr search failed: %s', exc)
            posts = []
        if posts:
            return posts
        return scrape_opengov_search_fallback(board['name'], keyword)
    return scrape_board(board['url'], board['name'], keyword)


def load_cache():
    """캐시 파일에서 데이터 로드"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return None

def save_cache(data):
    """캐시 파일에 데이터 저장"""
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_visitors():
    """방문자 데이터 로드 (DB 우선, 없으면 파일)"""
    today = get_korean_time().strftime('%Y-%m-%d')

    # DB 사용 가능 시
    if DATABASE_URL:
        try:
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)

            # 오늘 날짜 데이터 조회
            cursor.execute("SELECT * FROM visitors WHERE date = %s", (today,))
            row = cursor.fetchone()

            # 전체 누적 조회
            cursor.execute("SELECT SUM(today_count) as total FROM visitors")
            total_row = cursor.fetchone()

            cursor.close()
            conn.close()

            if row:
                return {
                    "today": row['today_count'],
                    "total": total_row['total'] or 0,
                    "date": today
                }
            else:
                # 오늘 데이터 없으면 새로 생성
                return {"today": 0, "total": total_row['total'] or 0, "date": today}

        except Exception as e:
            print(f"❌ DB 로드 오류: {e}")

    # 파일 fallback
    if os.path.exists(VISITORS_FILE):
        try:
            with open(VISITORS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"today": 0, "total": 0, "date": today}

def save_visitors(data):
    """방문자 데이터 저장 (DB 우선, 없으면 파일)"""
    if DATABASE_URL:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # UPSERT (INSERT or UPDATE)
            cursor.execute("""
                INSERT INTO visitors (date, today_count, total_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (date)
                DO UPDATE SET today_count = %s, total_count = %s
            """, (data['date'], data['today'], data['total'], data['today'], data['total']))

            conn.commit()
            cursor.close()
            conn.close()
            return
        except Exception as e:
            print(f"❌ DB 저장 오류: {e}")

    # 파일 fallback
    with open(VISITORS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def increment_visitor():
    """방문자 수 증가"""
    visitors = load_visitors()
    today = get_korean_time().strftime('%Y-%m-%d')

    # 날짜가 바뀌면 오늘 방문자 초기화
    if visitors.get('date') != today:
        visitors['today'] = 0
        visitors['date'] = today

    visitors['today'] += 1
    visitors['total'] += 1
    save_visitors(visitors)
    return visitors

def background_scrape():
    """백그라운드에서 크롤링 실행 (30분마다)"""
    print(f"[{get_korean_time().strftime('%Y-%m-%d %H:%M:%S')}] 백그라운드 크롤링 시작...")

    config = load_config()
    all_results = []
    integrated_feed = []

    for board in config.get('boards', []):
        kw = board.get('keyword', '')
        posts = scrape_configured_board(board)

        # 개별 게시판 결과 저장
        clean_posts = []
        for p in posts:
            integrated_feed.append(p.copy())
            p_copy = p.copy()
            p_copy.pop('dt_obj', None)
            clean_posts.append(p_copy)

        all_results.append({
            'name': board['name'],
            'url': board['url'],
            'keyword': kw,
            'posts': clean_posts[:POSTS_PER_BOARD]
        })

    # 중복 제거 (URL 기준)
    seen_links = set()
    unique_feed = []
    for p in integrated_feed:
        if p['link'] not in seen_links:
            seen_links.add(p['link'])
            unique_feed.append(p)

    # 통합 피드 최신순 정렬
    unique_feed.sort(key=lambda x: x['dt_obj'], reverse=True)
    for p in unique_feed:
        p.pop('dt_obj', None)

    # 캐시에 저장
    cache_data = {
        'success': True,
        'data': all_results,
        'latest_posts': unique_feed[:POSTS_PER_BOARD],
        'updated_at': get_korean_time().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_cache(cache_data)

    print(f"[{get_korean_time().strftime('%Y-%m-%d %H:%M:%S')}] 크롤링 완료! (게시판 {len(all_results)}개)")


# ==================== 항공 특가 텔레그램 알림 ====================

def send_telegram_message(text):
    """텔레그램 봇으로 메시지 전송 (4096자 제한에 맞춰 분할 전송)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        app.logger.warning('텔레그램 미설정: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수를 확인하세요.')
        return False

    api_url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    ok = True
    for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        chunk = text[i:i + TELEGRAM_MESSAGE_LIMIT]
        try:
            response = requests.post(
                api_url,
                json={
                    'chat_id': TELEGRAM_CHAT_ID,
                    'text': chunk,
                    'disable_web_page_preview': True,
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            app.logger.warning('텔레그램 전송 실패: %s', exc)
            ok = False
    return ok


def claim_new_deals(posts):
    """아직 알림을 보내지 않은 특가 글만 골라내고, 보낸 것으로 표시한다.

    반환: (새 글 목록, 최초 실행 여부)
    최초 실행 시에는 기존 글을 전부 '보낸 것'으로만 기록하고 알림은 보내지 않는다.
    (봇을 처음 켰을 때 옛날 글 수십 개가 한꺼번에 쏟아지는 것을 방지)
    """
    # 최초 실행 여부는 글 개수가 아니라 별도 표식(__seeded__)으로 판별한다.
    # (뽐뿌가 막혀 글이 0건이어도 최초 실행 시작 메시지가 정상적으로 나가도록)
    seed_marker = '__seeded__'

    if DATABASE_URL:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM sent_deals WHERE link = %s", (seed_marker,))
            first_run = cursor.fetchone() is None
            if first_run:
                cursor.execute(
                    """
                    INSERT INTO sent_deals (link, title, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (link) DO NOTHING
                    """,
                    (seed_marker, '알림 시작 표식', 'system'),
                )

            new_posts = []
            for post in posts:
                cursor.execute(
                    """
                    INSERT INTO sent_deals (link, title, source)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (link) DO NOTHING
                    """,
                    (post['link'], post['title'], post['source']),
                )
                if cursor.rowcount and not first_run:
                    new_posts.append(post)

            conn.commit()
            cursor.close()
            conn.close()
            return new_posts, first_run
        except Exception as e:
            print(f"❌ 특가 기록 DB 오류: {e}")

    # 파일 fallback (서버 재시작 시 초기화될 수 있으므로 DB 사용 권장)
    first_run = not os.path.exists(SENT_DEALS_FILE)
    seen = set()
    if not first_run:
        try:
            with open(SENT_DEALS_FILE, 'r', encoding='utf-8') as f:
                seen = set(json.load(f))
        except Exception:
            pass

    new_posts = [p for p in posts if p['link'] not in seen and p['link'] != seed_marker]
    seen.update(p['link'] for p in posts)
    seen.add(seed_marker)  # 글이 0건이어도 파일을 생성해 최초 실행 표식을 남긴다
    with open(SENT_DEALS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
    return ([] if first_run else new_posts), first_run


def format_deal_alert(new_posts):
    """새 특가 글 목록을 텔레그램 메시지 텍스트로 변환한다."""
    shown = new_posts[:MAX_DEALS_PER_ALERT]
    lines = [f"✈️ 새 항공 특가 {len(new_posts)}건!"]
    for post in shown:
        lines.append("")
        lines.append(f"🔥 [{post['source']}] {post['title']}")
        lines.append(post['link'])
    if len(new_posts) > len(shown):
        lines.append("")
        lines.append(f"…외 {len(new_posts) - len(shown)}건")
    return "\n".join(lines)


def check_airline_deals():
    """특가 게시판을 수집해 새 글을 텔레그램으로 알린다. (스케줄러에서 주기 실행)"""
    config = load_config()
    deal_boards = config.get('deal_boards', [])
    if not deal_boards:
        return []

    print(f"[{get_korean_time().strftime('%Y-%m-%d %H:%M:%S')}] 항공 특가 확인 중...")

    posts_by_link = {}
    for board in deal_boards:
        for post in scrape_configured_board(board):
            posts_by_link.setdefault(post['link'], post)
    posts = sorted(posts_by_link.values(), key=lambda p: p['dt_obj'], reverse=True)

    # 대시보드 확인용 캐시 저장
    clean_posts = []
    for p in posts:
        c = p.copy()
        c.pop('dt_obj', None)
        clean_posts.append(c)
    try:
        with open(DEALS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'success': True,
                'deals': clean_posts,
                'updated_at': get_korean_time().strftime('%Y-%m-%d %H:%M:%S'),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 특가 캐시 저장 오류: {e}")

    new_posts, first_run = claim_new_deals(posts)

    if first_run:
        # 시작 인사는 보내지 않는다. DB가 없으면 재시작/배포 때마다 최초 실행으로
        # 판정되어 같은 인사가 반복 전송되기 때문. 기존 글은 조용히 기록만 한다.
        # (연결 확인은 /api/telegram/test 사용)
        print(f"항공 특가 알림 최초 실행: 기존 {len(posts)}건 기록 완료 (알림 없이 시작)")
        return []

    if not posts:
        print("항공 특가 확인 완료: 수집된 글 없음 (게시판 접근 차단 여부 확인 필요)")
        return []

    if new_posts:
        send_telegram_message(format_deal_alert(new_posts))
        print(f"항공 특가 알림 전송: 새 글 {len(new_posts)}건")
    else:
        print("항공 특가 확인 완료: 새 글 없음")

    return new_posts


# gunicorn 배포에서는 __main__ 블록이 실행되지 않으므로 모듈 로드 시점에
# 테이블을 준비한다. (없으면 sent_deals 기록이 파일로만 남아 배포 때마다
# 초기화되고, 시작 메시지가 반복 전송된다)
init_db()

# gunicorn 배포에서도 알림이 돌도록 모듈 로드 시점에 잡을 등록한다.
# (텔레그램 미설정 상태나 테스트 실행 중에는 등록하지 않음)
if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    scheduler.add_job(
        func=check_airline_deals,
        trigger="interval",
        minutes=DEAL_CHECK_INTERVAL_MINUTES,
        next_run_time=get_korean_time() + timedelta(seconds=30),
        id='deal_alert_job',
        name='항공 특가 확인 및 텔레그램 알림',
        replace_existing=True,
    )


@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', boards=config.get('boards', []))

@app.route('/api/scrape_all')
def api_scrape_all():
    """캐시된 데이터 반환 (캐시 없으면 즉시 크롤링)"""
    cache = load_cache()

    if cache:
        return jsonify(cache)

    # 캐시 없으면 즉시 크롤링
    background_scrape()
    cache = load_cache()

    if cache:
        return jsonify(cache)

    # 그래도 없으면 빈 데이터 반환
    return jsonify({
        'success': True,
        'data': [],
        'latest_posts': [],
        'updated_at': get_korean_time().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/api/boards', methods=['POST', 'DELETE'])
def manage_boards():
    data = request.json
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403

    config = load_config()
    if request.method == 'POST':
        config['boards'].append({'name': data['name'], 'url': data['url'], 'keyword': data.get('keyword', '')})
    elif request.method == 'DELETE':
        config['boards'] = [b for b in config['boards'] if b['url'] != data['url']]
    save_config(config)
    return jsonify({'success': True})

@app.route('/api/refresh', methods=['POST'])
def refresh_data():
    """즉시 크롤링 실행"""
    print("수동 새로고침 요청 받음")
    background_scrape()
    cache = load_cache()
    return jsonify(cache if cache else {'success': False, 'message': 'Refresh failed'})

@app.route('/api/deals')
def api_deals():
    """최근 수집된 항공 특가 목록 반환"""
    if os.path.exists(DEALS_CACHE_FILE):
        try:
            with open(DEALS_CACHE_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception:
            pass
    return jsonify({'success': True, 'deals': [], 'updated_at': None})

@app.route('/api/deals/check', methods=['GET', 'POST'])
def api_deals_check():
    """즉시 특가 확인 + 새 글 있으면 텔레그램 전송 (테스트/수동 실행용)

    브라우저에서 바로 실행: /api/deals/check?pw=1111
    """
    if request.method == 'POST':
        password = (request.json or {}).get('password')
    else:
        password = request.args.get('pw')
    if password != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403

    new_posts = check_airline_deals()
    return jsonify({
        'success': True,
        'new_deals': len(new_posts),
        'telegram_configured': bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.route('/api/deals/debug')
def api_deals_debug():
    """특가 소스별 수집 상태 진단. 브라우저에서 ?pw=1111 로 확인."""
    if request.args.get('pw') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403

    report = []
    for board in load_config().get('deal_boards', []):
        entry = {'name': board.get('name'), 'url': board.get('url')}
        if board.get('type') == 'naver_cafe':
            entry['api_configured'] = bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET)
        elif board.get('url'):
            try:
                response = requests.get(
                    board['url'], headers=get_headers(board['url']),
                    verify=False, timeout=15,
                )
                entry['http_status'] = response.status_code
                entry['response_bytes'] = len(response.content)
            except requests.RequestException as exc:
                entry['http_status'] = None
                entry['fetch_error'] = str(exc)

        try:
            posts = scrape_configured_board(board)
            entry['posts_found'] = len(posts)
            entry['sample_titles'] = [p['title'] for p in posts[:3]]
        except Exception as exc:
            entry['posts_found'] = 0
            entry['scrape_error'] = str(exc)

        # 키워드 필터를 끈 원본 수집 결과 (파싱/인코딩 정상 여부 확인용)
        try:
            raw_board = dict(board, keyword='')
            raw_posts = scrape_configured_board(raw_board)
            entry['raw_posts_found'] = len(raw_posts)
            entry['raw_titles'] = [p['title'] for p in raw_posts[:5]]
        except Exception as exc:
            entry['raw_error'] = str(exc)
        report.append(entry)

    return jsonify({
        'success': True,
        'telegram_configured': bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        'database_configured': bool(DATABASE_URL),
        # 환경변수 이름 오타 진단용: 관련 변수의 '이름'만 노출한다 (값은 비공개)
        'env_keys_seen': sorted(
            k for k in os.environ
            if 'NAVER' in k.upper() or 'TELEGRAM' in k.upper()
        ),
        'boards': report,
    })

@app.route('/api/telegram/test', methods=['GET', 'POST'])
def api_telegram_test():
    """텔레그램 봇 연결 테스트 메시지 전송.

    브라우저에서 바로 확인할 수 있도록 GET 도 허용한다.
    예) https://<도메인>/api/telegram/test?pw=1111
    """
    if request.method == 'POST':
        password = (request.json or {}).get('password')
    else:
        password = request.args.get('pw')
    if password != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return jsonify({
            'success': False,
            'message': 'TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.',
        })

    ok = send_telegram_message('✅ 제제보드 항공 특가 알림 테스트 메시지입니다!')
    return jsonify({
        'success': ok,
        'message': '텔레그램으로 테스트 메시지를 보냈습니다. 봇 채팅방을 확인하세요.'
        if ok else '텔레그램 전송에 실패했습니다. 봇 토큰/chat_id를 확인하세요.',
    })

@app.route('/api/visitors', methods=['GET', 'POST'])
def visitors():
    """방문자 수 관리"""
    if request.method == 'POST':
        # 방문자 카운트 증가
        visitors_data = increment_visitor()
        return jsonify(visitors_data)
    else:
        # 방문자 수 조회
        visitors_data = load_visitors()
        return jsonify(visitors_data)

if __name__ == '__main__':
    # DB 초기화
    print("DB 초기화 중...")
    init_db()

    # 앱 시작 시 즉시 한 번 크롤링
    print("앱 시작! 초기 크롤링 실행 중...")
    background_scrape()

    # 30분마다 크롤링 스케줄 등록
    scheduler.add_job(
        func=background_scrape,
        trigger="interval",
        minutes=30,
        id='scrape_job',
        name='30분마다 게시판 크롤링',
        replace_existing=True
    )
    print("스케줄러 등록 완료! 30분마다 크롤링합니다.")

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)