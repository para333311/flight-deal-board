import json
import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from datetime import datetime
from urllib.parse import urljoin
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
ADMIN_PASSWORD = "1111" # 기본 비밀번호
DATABASE_URL = os.environ.get('DATABASE_URL')  # Render에서 자동으로 제공
OPENGOV_HOST = 'opengov.seoul.go.kr'
NAVER_WEB_SEARCH_URL = 'https://search.naver.com/search.naver'
OPENGOV_DOC_ID_RE = re.compile(r'opengov\.seoul\.go\.kr/sanction/(\d+)')

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
    if not date_str:
        return datetime(1900, 1, 1)
    try:
        clean_date = re.sub(r'[^0-9-]', '-', date_str.replace('.', '-')).strip('-')
        parts = clean_date.split('-')
        if len(parts[0]) == 2:
            parts[0] = '20' + parts[0]
        return datetime.strptime("-".join(parts[:3]), '%Y-%m-%d')
    except (TypeError, ValueError, IndexError):
        return datetime(1900, 1, 1)


def split_keywords(keyword):
    """관리자 입력 문자열을 OR 검색어 목록으로 변환한다.

    기존 설정은 ``재개발.신속통합.일대``처럼 마침표를 구분자로
    사용하므로 마침표, 쉼표, 세로줄, 줄바꿈을 모두 구분자로 처리한다.
    """
    if not keyword:
        return ()
    return tuple(
        dict.fromkeys(
            term.strip()
            for term in re.split(r'[.,|·\n]+', keyword)
            if term.strip()
        )
    )


def title_matches_keywords(title, keyword):
    keywords = split_keywords(keyword)
    return not keywords or any(term in title for term in keywords)


def scrape_board(url, name, keyword):
    posts = []
    try:
        session = requests.Session()
        response = session.get(url, headers=get_headers(url), verify=False, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        rows = soup.select('table tbody tr, .board-list tr, .bbs-list tr, .list_type li, .news-list li, .search-result-list li, .list-wrap li')
        if not rows:
            rows = soup.select('.title, .subject, .txt_left, .tit')

        for row in rows:
            title_elem = row.select_one('a, .tit, .subject, .title')
            if not title_elem: continue
            
            title = title_elem.get_text(strip=True)
            if len(title) < 3: continue
            
            # 설정의 구분된 검색어 중 하나라도 제목에 있으면 표시한다.
            if not title_matches_keywords(title, keyword):
                continue
            
            link = title_elem.get('href', '')
            if not link or '#' in link or 'javascript' in link:
                parent_a = row.find_parent('a') or row.find('a')
                if parent_a: link = parent_a.get('href', '')

            full_link = urljoin(url, link)
            
            date_val = ""
            for elem in row.select('td, span, .date, .reg_date, .day'):
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
        
    except requests.RequestException as e:
        app.logger.warning("Error scraping %s (%s): %s", name, url, e)
    except Exception:
        app.logger.exception("Unexpected scraping error for %s (%s)", name, url)
    
    return posts


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
            date_value = date_match.group() if date_match else ''
            posts_by_id[document_id] = {
                'title': title,
                'link': f'https://{OPENGOV_HOST}/sanction/{document_id}',
                'date': date_value,
                'dt_obj': parse_date(date_value),
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


def scrape_configured_board(board):
    """게시판을 수집하고 정보소통광장만 검색 색인으로 자동 우회한다."""
    keyword = board.get('keyword', '')
    posts = scrape_board(board['url'], board['name'], keyword)
    if not posts and OPENGOV_HOST in board.get('url', ''):
        posts = scrape_opengov_search_fallback(board['name'], keyword)
    return posts


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
            'posts': clean_posts[:15]
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
        'latest_posts': unique_feed[:30],
        'updated_at': get_korean_time().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_cache(cache_data)

    print(f"[{get_korean_time().strftime('%Y-%m-%d %H:%M:%S')}] 크롤링 완료! (게시판 {len(all_results)}개)")

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