# job-watch — 금융권 채용 공고 감시기

잡알리오 오픈API + 신용평가사 + 시중은행 + 보증기관의 공식 채용 페이지를 하루 두 번 확인해,
희망 직무(데이터·리스크·신용평가모형)와 공채 공고를 골라 텔레그램으로 보내고
정적 대시보드로 정리합니다.

```
watch.py       실행 진입점 (run / probe-alio / probe-site / render)
collectors.py  잡알리오 API + 채용 페이지 수집
matcher.py     3단 티어 키워드 매칭
render.py      docs/index.html 대시보드 생성
notify.py      텔레그램 알림 (실패 시 콘솔)
config.yaml    감시 대상과 키워드 — 여기만 고치면 됨
```

## 설치

```bash
git clone <your-repo> && cd job-watch
pip install -r requirements.txt
```

## 1단계 — 잡알리오 인증키 발급

1. [공공데이터포털](https://www.data.go.kr) 가입
2. "공공기관 채용정보" 활용신청 → 인증키 발급 (보통 즉시 승인)
3. 환경변수로 등록

```bash
export ALIO_SERVICE_KEY="발급받은키"
```

**중요**: `config.yaml`의 `alio.endpoint`와 `field_map`은 확인이 필요한 기본값입니다.
활용신청 후 Swagger 문서에서 실제 필드명을 확인하고, 아래 명령으로 응답 원문을 대조하세요.

```bash
python watch.py probe-alio
```

출력된 JSON 구조를 보고 `result_path`(공고 배열이 들어있는 경로)와
`field_map`(응답 필드명 → 내부 필드명)을 맞춰 넣으면 됩니다.

## 2단계 — 사이트 모드 판별

민간 채용 페이지는 절반 이상이 자바스크립트로 그려집니다.
정적 HTML을 긁으면 빈 결과가 나오므로, 사이트별로 먼저 확인하세요.

```bash
python watch.py probe-site kb_bank
python watch.py probe-site korea_ratings
```

`verdict`가 "SPA로 보임"이면 `config.yaml`에서 해당 사이트를 `mode: snapshot`으로 바꿉니다.
snapshot 모드는 공고 제목을 못 읽는 대신, **페이지가 바뀌면 무조건 알려줍니다.**
공고를 놓치지 않는다는 목적에는 충분합니다.

## 3단계 — 텔레그램 봇

1. 텔레그램에서 `@BotFather` → `/newbot` → 토큰 받기
2. 만든 봇에게 아무 메시지나 보내기
3. `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `chat.id` 확인

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## 4단계 — 실행

```bash
python watch.py run --dry-run   # 알림 없이 결과만 확인
python watch.py run             # 실제 수집 + 알림 + 보드 갱신
open docs/index.html
```

## 5단계 — 자동화 (GitHub Actions)

1. GitHub 저장소 생성 후 푸시
2. Settings → Secrets and variables → Actions 에 등록
   - `ALIO_SERVICE_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Settings → Pages → Source를 **GitHub Actions**로 설정
4. Actions 탭에서 `job-watch` → Run workflow 로 첫 실행

이후 한국시간 08:30 / 18:30 에 자동 실행되고, 보드는
`https://<사용자명>.github.io/<저장소명>/` 에 게시됩니다.

**주의**: GitHub Actions 러너는 해외 IP입니다. 일부 국내 기관 사이트가
해외 접속을 차단하면 해당 소스만 실패합니다(다른 소스는 계속 동작).
로그를 보고 막히는 곳이 있으면 그 사이트는 로컬 cron으로 돌리세요.

## 필터 조정

`config.yaml`의 키워드 티어만 고치면 됩니다.

| 항목 | 역할 |
|---|---|
| `keywords.core` | 희망 직무의 핵심. 하나만 걸려도 최우선 |
| `keywords.open_recruitment` | 공채 신호. 감시 대상 기업이면 직무 무관 통과 |
| `keywords.related` | 인접 직무. 다른 신호와 겹칠 때만 |
| `keywords.exclude` | 확실한 노이즈만 |
| `watchlist_institutions` | 이 기관 공고는 직무 무관 전부 수집 |

**설계 원칙**: 미탐이 오탐보다 비쌉니다. 알림이 많아서 불편한 것보다
연 1회 열리는 공고를 놓치는 게 훨씬 큰 손실입니다. 키워드를 좁히기 전에
한 시즌은 넓게 돌려보세요.

## 수집 범위와 예절

- 기관·기업의 **공식 채용 페이지와 공개 API만** 수집합니다.
- 사람인·잡코리아·인크루트 등 상용 채용 플랫폼의 검색 결과 크롤링은
  이용약관 위반 소지가 있어 대상에서 제외했습니다.
  (단 `ibk.incruit.com`처럼 기관이 자사 채용 홈으로 운영하는 주소는 공식 채널입니다.)
- 요청 간 2초 간격, User-Agent에 연락처를 명시합니다. `config.yaml`의
  `fetch.user_agent`에서 이메일을 본인 것으로 바꿔주세요.
