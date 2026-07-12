# JEJE BOARD

여러 게시판(및 네이버블로그)의 새 글을 한 화면에서 모아보고, 텔레그램으로 새 글 알림을 받을 수 있는 Flask 앱입니다.

## 네이버블로그 구독

게시판 등록 시 주소에 네이버블로그 주소를 입력하면 자동으로 RSS(`https://rss.blog.naver.com/{blogId}.xml`)를 통해 글 목록을 가져옵니다. 지원 형식:

- `https://blog.naver.com/블로그아이디`
- `https://m.blog.naver.com/블로그아이디`
- `https://blog.naver.com/PostList.naver?blogId=블로그아이디`

## 텔레그램 새 글 알림 설정

1. [BotFather](https://t.me/BotFather)로 봇을 생성하고 토큰을 발급받습니다.
2. 배포 환경에 환경변수를 설정합니다.
   - `TELEGRAM_BOT_TOKEN`: 발급받은 봇 토큰 (필수)
   - `CHECK_INTERVAL_SECONDS`: 새 글 확인 주기(초), 기본값 600
   - `ENABLE_BACKGROUND_CHECKER`: 앱 내부 백그라운드 스레드로 주기 확인을 할지 여부(`1`/`0`), 기본값 `1`. **워커가 여러 개인 배포 환경(예: gunicorn multi-worker)에서는 `0`으로 끄고, 아래 `/api/check_updates`를 외부 크론으로 호출하세요.**
3. 앱 배포 후, 관리자 비밀번호로 웹훅을 등록합니다.

   ```bash
   curl -X POST https://your-app-domain/api/telegram/set_webhook \
     -H "Content-Type: application/json" \
     -d '{"password": "관리자비밀번호", "webhook_url": "https://your-app-domain/telegram/webhook"}'
   ```

4. 텔레그램에서 봇을 찾아 `/start`를 보내면 새 글 알림 구독이 시작됩니다. `/stop`으로 해제, `/list`로 등록된 게시판 목록을 확인할 수 있습니다.
5. 새로 등록한 게시판은 첫 확인 시에는 알림 없이 기준선만 저장하고, 그 다음 확인부터 새 글이 감지되면 알림을 보냅니다.

### 외부 크론으로 확인 주기 트리거하기

내부 백그라운드 스레드 대신 외부 스케줄러(Render Cron Job, cron-job.org 등)로 확인을 트리거하려면 아래 엔드포인트를 주기적으로 호출하세요.

```
GET /api/check_updates
```
