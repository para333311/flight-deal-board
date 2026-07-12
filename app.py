import json
import os
import re
import requests
import threading
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, parse_qs
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

CONFIG_FILE = 'config.json'
ADMIN_PASSWORD = "1111" # 기본 비밀번호

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHECK_INTERVAL_SECONDS = int(os.environ.get('CHECK_INTERVAL_SECONDS', '600'))
ENABLE_BACKGROUND_CHECKER = os.environ.get('ENABLE_BACKGROUND_CHECKER', '1') == '1'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}
    config.setdefault('boards', [])
    config.setdefault('telegram_subscribers', [])
    return config

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

def extract_naver_blog_id(url):
    if 'blog.naver.com' not in url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if 'blogId' in qs and qs['blogId'][0]:
        return qs['blogId'][0]
    path_parts = [p for p in parsed.path.split('/') if p]
    reserved = {'postlist.naver', 'postlist.nhn', 'postview.naver', 'postview.nhn', 'prologuelist.naver'}
    if path_parts and path_parts[0].lower() not in reserved:
        return path_parts[0]
    return None

def parse_rss_date(date_str):
    if not date_str:
        return datetime(1900, 1, 1)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return datetime(1900, 1, 1)

def scrape_naver_blog_rss(blog_id, name, keyword):
    posts = []
    rss_url = f'https://rss.blog.naver.com/{blog_id}.xml'
    try:
        response = requests.get(rss_url, headers=get_headers(rss_url), verify=False, timeout=15)
        response.encoding = 'utf-8'
        root = ET.fromstring(response.text)

        for item in root.findall('.//item'):
            title = (item.findtext('title') or '').strip()
            link = (item.findtext('link') or '').strip()
            if not title or not link:
                continue

            if keyword and keyword.strip() and keyword.strip() not in title:
                continue

            dt_obj = parse_rss_date((item.findtext('pubDate') or '').strip())
            posts.append({
                'title': title,
                'link': link,
                'date': dt_obj.strftime('%Y-%m-%d') if dt_obj.year > 1900 else '',
                'dt_obj': dt_obj,
                'source': name
            })

        posts.sort(key=lambda x: x['dt_obj'], reverse=True)

    except Exception as e:
        print(f"Error scraping naver blog rss {blog_id}: {e}")

    return posts

def scrape_board(url, name, keyword):
    blog_id = extract_naver_blog_id(url)
    if blog_id:
        return scrape_naver_blog_rss(blog_id, name, keyword)

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

def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': False},
            timeout=10
        )
        return True
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False

def check_new_posts_and_notify():
    config = load_config()
    subscribers = config.get('telegram_subscribers', [])
    changed = False

    for board in config.get('boards', []):
        kw = board.get('keyword', '')
        posts = scrape_board(board['url'], board['name'], kw)
        if not posts:
            continue

        last_links = board.get('last_links')
        if last_links is None:
            # 최초 등록 시에는 알림 없이 기준선만 저장
            board['last_links'] = [p['link'] for p in posts[:30]]
            changed = True
            continue

        seen = set(last_links)
        new_posts = [p for p in posts if p['link'] not in seen]
        if new_posts:
            if subscribers:
                for p in reversed(new_posts):  # 오래된 글부터 순서대로 발송
                    text = f"🔔 [{p['source']}] 새 글 등록\n{p['title']}\n{p['link']}"
                    for chat_id in subscribers:
                        send_telegram_message(chat_id, text)
            board['last_links'] = [p['link'] for p in posts[:30]]
            changed = True

    if changed:
        save_config(config)

def background_checker():
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        try:
            check_new_posts_and_notify()
        except Exception as e:
            print(f"Background check error: {e}")

_checker_started = False
def start_background_checker():
    global _checker_started
    if _checker_started or not ENABLE_BACKGROUND_CHECKER:
        return
    _checker_started = True
    t = threading.Thread(target=background_checker, daemon=True)
    t.start()

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
        config['boards'].append({
            'name': data['name'],
            'url': data['url'],
            'keyword': data.get('keyword', ''),
            'last_links': None  # 최초 체크 시 알림 없이 기준선만 저장
        })
    elif request.method == 'DELETE':
        config['boards'] = [b for b in config['boards'] if b['url'] != data['url']]
    save_config(config)
    return jsonify({'success': True})

@app.route('/api/check_updates')
def api_check_updates():
    """외부 크론(예: Render Cron Job, cron-job.org)에서 주기적으로 호출해 새 글을 확인하고 텔레그램으로 알림을 보낸다."""
    check_new_posts_and_notify()
    return jsonify({'success': True})

@app.route('/api/telegram/info')
def api_telegram_info():
    config = load_config()
    return jsonify({
        'configured': bool(TELEGRAM_BOT_TOKEN),
        'subscriber_count': len(config.get('telegram_subscribers', []))
    })

@app.route('/api/telegram/set_webhook', methods=['POST'])
def api_telegram_set_webhook():
    data = request.json or {}
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({'success': False, 'message': 'Password Denied'}), 403
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({'success': False, 'message': 'TELEGRAM_BOT_TOKEN not set'}), 400
    webhook_url = data.get('webhook_url')
    if not webhook_url:
        return jsonify({'success': False, 'message': 'webhook_url required'}), 400

    resp = requests.post(
        f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook',
        json={'url': webhook_url},
        timeout=10
    )
    return jsonify(resp.json())

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get('message', {})
    chat_id = message.get('chat', {}).get('id')
    text = (message.get('text') or '').strip()

    if not chat_id:
        return jsonify({'ok': True})

    config = load_config()
    subscribers = config.setdefault('telegram_subscribers', [])

    if text == '/start':
        if chat_id not in subscribers:
            subscribers.append(chat_id)
            save_config(config)
        send_telegram_message(chat_id, '네이버 블로그 등 등록된 게시판의 새 글 알림 구독이 시작되었습니다.\n중지하려면 /stop, 구독중인 목록을 보려면 /list 를 입력하세요.')
    elif text == '/stop':
        if chat_id in subscribers:
            subscribers.remove(chat_id)
            save_config(config)
        send_telegram_message(chat_id, '구독이 해제되었습니다.')
    elif text == '/list':
        names = [b['name'] for b in config.get('boards', [])]
        body = '\n'.join(f'- {n}' for n in names) if names else '등록된 게시판이 없습니다.'
        send_telegram_message(chat_id, f'구독중인 게시판 목록:\n{body}')

    return jsonify({'ok': True})

start_background_checker()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)