import json
import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from datetime import datetime
from urllib.parse import urljoin, quote
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

CONFIG_FILE = 'config.json'
ADMIN_PASSWORD = "1111" # 기본 비밀번호

# 서울정보소통광장(opengov.seoul.go.kr) 전용 설정
# 결재문서 검색 결과 페이지. {keyword} 자리에 검색어가 들어갑니다.
# 만약 사이트의 검색 파라미터명이 다르면 이 템플릿만 수정하면 됩니다.
SEOUL_SEARCH_TEMPLATE = 'https://opengov.seoul.go.kr/sanction/list?searchText={keyword}'
# 결재문서/제안/뉴스 등 상세 페이지 링크 패턴 → 검색 결과 항목을 안정적으로 식별
SEOUL_DOC_PATTERN = re.compile(r'/(?:sanction|proaction|mediahub|announce|news|civilappeal|budget)/\d+')

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

def scrape_board(url, name, keyword):
    posts = []
    try:
        session = requests.Session()
        response = session.get(url, headers=get_headers(url), verify=False, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        rows = soup.select('table tbody tr, .board-list tr, .bbs-list tr, .list_type li, .news-list li, .search-result-list li, .list-wrap li')
        if not rows:
            rows = soup.select('.title, .subject, .txt_left, .tit')

        for row in rows:
            title_elem = row.select_one('a, .tit, .subject, .title')
            if not title_elem: continue
            
            title = title_elem.get_text(strip=True)
            if len(title) < 3: continue
            
            # 키워드 필터링
            if keyword and keyword.strip():
                if keyword.strip() not in title:
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

    except Exception as e:
        print(f"Error scraping {name}: {e}")

    return posts


def build_seoul_url(keyword):
    """검색어로 서울정보소통광장 검색 URL을 생성"""
    return SEOUL_SEARCH_TEMPLATE.format(keyword=quote(keyword.strip()))


def scrape_seoul_opengov(base_url, name, pages=2):
    """
    서울정보소통광장(opengov.seoul.go.kr) 검색 결과 전용 스크래퍼.
    base_url에는 이미 검색어가 포함된 검색 결과 URL이 들어온다.
    결재문서 상세 링크 패턴(/sanction/123... 등)으로 항목을 식별하므로
    사이트의 CSS 클래스가 바뀌어도 비교적 안정적으로 동작한다.
    """
    posts = []
    seen = set()
    session = requests.Session()

    for page in range(1, pages + 1):
        sep = '&' if '?' in base_url else '?'
        page_url = f"{base_url}{sep}page={page}"
        try:
            response = session.get(page_url, headers=get_headers('https://opengov.seoul.go.kr/'),
                                   verify=False, timeout=15)
            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
        except Exception as e:
            print(f"[Seoul] {name} page {page} error: {e}")
            break

        # 1차: 상세문서 링크 패턴으로 결과 항목 추출 (가장 안정적)
        anchors = soup.find_all('a', href=SEOUL_DOC_PATTERN)
        # 2차: 패턴이 안 맞으면 일반적인 목록 셀렉터로 폴백
        if not anchors:
            anchors = soup.select('.result-list a, .view-list a, table tbody tr a, '
                                  '.board-list a, .list li a, .search-list a')

        page_found = 0
        for a in anchors:
            title = a.get_text(strip=True)
            href = a.get('href', '')
            if len(title) < 3 or not href or 'javascript' in href:
                continue

            full_link = urljoin('https://opengov.seoul.go.kr/', href)
            if full_link in seen:
                continue
            seen.add(full_link)

            # 상위 행/블록에서 날짜·부서 정보 추출
            container = a.find_parent(['tr', 'li', 'dl', 'div']) or a
            block_text = container.get_text(" ", strip=True)

            date_val = ""
            m = re.search(r'\d{4}[-.]\s?\d{1,2}[-.]\s?\d{1,2}', block_text)
            if m:
                date_val = m.group().replace(' ', '')

            dept = ""
            dept_elem = container.select_one('.department, .dept, .org, .part, .division')
            if dept_elem:
                dept = dept_elem.get_text(strip=True)

            posts.append({
                'title': title,
                'link': full_link,
                'date': date_val,
                'dept': dept,
                'dt_obj': parse_date(date_val),
                'source': name
            })
            page_found += 1

        # 이 페이지에서 결과가 하나도 없으면 더 이상 페이지가 없는 것으로 판단
        if page_found == 0:
            break

    posts.sort(key=lambda x: x['dt_obj'], reverse=True)
    return posts

@app.route('/')
def index():
    config = load_config()
    return render_template('index.html', boards=config.get('boards', []))

@app.route('/api/scrape_all')
def api_scrape_all():
    config = load_config()
    all_results = []
    integrated_feed = [] # 통합 피드용
    
    for board in config.get('boards', []):
        kw = board.get('keyword', '')
        # 서울정보소통광장 전용 스크래퍼로 분기
        if board.get('type') == 'seoul':
            posts = scrape_seoul_opengov(board['url'], board['name'])
        else:
            posts = scrape_board(board['url'], board['name'], kw)
        
        # 개별 게시판 결과 저장
        clean_posts = []
        for p in posts:
            integrated_feed.append(p.copy()) # 통합 피드에 추가
            p.pop('dt_obj', None) # JSON 전송을 위해 제거
            clean_posts.append(p)

        all_results.append({
            'name': board['name'],
            'url': board['url'],
            'keyword': kw,
            'type': board.get('type', ''),
            'posts': clean_posts[:15]
        })
    
    # 통합 피드 최신순 정렬 및 상위 30개 추출
    integrated_feed.sort(key=lambda x: x['dt_obj'], reverse=True)
    for p in integrated_feed: p.pop('dt_obj', None)
    
    return jsonify({
        'success': True,
        'data': all_results,
        'latest_posts': integrated_feed[:30],
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/api/boards', methods=['POST', 'DELETE'])
def manage_boards():
    data = request.json
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403
    
    config = load_config()
    if request.method == 'POST':
        board_type = data.get('type', '')
        if board_type == 'seoul':
            # 서울정보소통광장: 검색어만 입력받아 검색 URL을 자동 생성
            keyword = (data.get('keyword') or '').strip()
            if not keyword:
                return jsonify({'success': False, 'message': '검색어를 입력하세요'}), 400
            url = data.get('url') or build_seoul_url(keyword)
            name = data.get('name') or f'서울소통광장: {keyword}'
            # 같은 검색어 중복 방지
            if any(b.get('url') == url for b in config['boards']):
                return jsonify({'success': False, 'message': '이미 등록된 검색어입니다'}), 409
            config['boards'].append({'name': name, 'url': url, 'keyword': keyword, 'type': 'seoul'})
        else:
            config['boards'].append({'name': data['name'], 'url': data['url'], 'keyword': data.get('keyword', '')})
    elif request.method == 'DELETE':
        config['boards'] = [b for b in config['boards'] if b['url'] != data['url']]
    save_config(config)
    return jsonify({'success': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)