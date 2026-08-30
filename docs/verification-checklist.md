# Общая проверка: фазы 1–3 одним прогоном

Один файл со всеми проверками того, что построено по
[спеке 2026-08-30](superpowers/specs/2026-08-30-backtest-and-tv-eyes-design.md):
бэктестер (Фаза 1), отчёты и автопсия (Фаза 2), TradingView-глаза (Фаза 3).

## Инструкция

- Иди по блокам сверху вниз: **A** — без сети и ключей, **B** — нужен
  интернет (и ключ для форекса), **C** — нужен TradingView Desktop,
  **D** — контроль, что боевой бот не задет.
- Каждая проверка: команда → «Жду:» (ожидаемый результат). Совпало — ставь
  `[x]`. Не совпало — **не чини сам**: скопируй полный вывод и пришли мне
  вместе с номером проверки, я разберусь.
- Всё выполняется в корне репозитория, в ветке
  `claude/tradingview-smart-money-signals-5ndydk` (или в master после мержа).
- Блоки B и C независимы: можно проверить B сегодня, C — когда поставишь
  TradingView.

---

## Блок A — код и конвейер (любая машина, ~2 минуты, без сети)

- [ ] **A1. Зависимости**
  ```bash
  pip install -r requirements.txt pytest pytest-asyncio flake8
  ```
  Жду: ставится без ошибок (предупреждения pip — норм).

- [ ] **A2. Тесты**
  ```bash
  pytest tests/ -q
  ```
  Жду: `8xx passed` (сейчас 845), **0 failed** — в любой день недели,
  выходные включительно.

- [ ] **A3. Линт**
  ```bash
  flake8 app/ tests/ smc_watcher.py smc_backtest.py
  ```
  Жду: пустой вывод.

- [ ] **A4. Самотест бэктестера** — конвейер целиком на детерминированной
  синтетике: движок → дедуп → журнал → отчёт.
  ```bash
  python smc_backtest.py --selftest
  ```
  Жду: шесть строк `OK` и `SELFTEST OK` в конце, ~5–20 секунд. Цифры в
  выводе синтетические — о стратегии они не говорят ничего.

## Блок B — реальный бэктест (интернет; ключ — только для форекса)

- [ ] **B1. ETHUSD за год** (ключ не нужен — Binance):
  ```bash
  python smc_backtest.py --pair ETHUSD --days 365 --report-dir data/backtest/reports
  ```
  Жду: строки `ETHUSD h4/h1/m5: N candles` в stderr, затем отчёт с шапкой
  `Backtest ETHUSD [conservative]` и строкой `NOT simulated: …`; файл
  `data/backtest/reports/<дата>-ETHUSD.txt`. Первый запуск качает историю
  пару минут; повторный — мгновенно из кэша `data/backtest/`.

- [ ] **B2. USDJPY за год** (нужен `TWELVEDATA_API_KEY` — тот же, что у
  боевого бота):
  ```bash
  TWELVEDATA_API_KEY=твой_ключ python smc_backtest.py --pair USDJPY --days 365 --report-dir data/backtest/reports
  ```
  Жду: то же самое для USDJPY. Скачивание истории — ~15 запросов к Twelve
  Data (лимитер сам держит темп 8/мин, это займёт пару минут).

- [ ] **B3. Обе пары разом + сводка**:
  ```bash
  TWELVEDATA_API_KEY=твой_ключ python smc_backtest.py --days 365 --report-dir data/backtest/reports
  ```
  Жду: два отчёта из кэша (быстро) и блок `COMBINED` с строками ETHUSD,
  USDJPY и TOTAL.

- [ ] **B4. Прислать отчёты мне** — оба файла из
  `data/backtest/reports/`. Как читать секции — `docs/backtest-runbook.md`;
  разбор цифр и выводы по ⭐-условиям и новым парам я сделаю по этим файлам.

## Блок C — TradingView-глаза (после установки по инструкции)

Установка: `docs/tradingview-mcp-setup.md` (бесплатный аккаунт TV +
Desktop + мост + MCP-конфиг локального Claude Code).

- [ ] **C1. Мост жив**: TradingView запущен через debug-скрипт; в Claude
  Code в папке репо: *«Use tv_health_check to verify TradingView is
  connected»*. Жду: ответ, что соединение установлено.

- [ ] **C2. Управление графиком**: *«Открой OANDA:USDJPY на H1»*. Жду: в
  приложении TradingView сменился символ и таймфрейм.

- [ ] **C3. Скилл verify-setup**: скажи *«проверь сетап USDJPY»* (или
  вставь текст любого 🚨-алерта). Жду: Claude сам пройдёт по чеклисту —
  H4 → H1 (LuxAlgo, если добавлен) → M5, нарисует уровни алерта, приложит
  скриншоты и даст вердикт `✅ / ⚠️ / ⛔` по-русски. Он не торгует и не
  ставит TV-алерты — только читает и рисует.

## Блок D — боевой бот не задет

- [ ] **D1. Диф ветки** (до мержа):
  ```bash
  git fetch origin master && git diff --stat origin/master...HEAD
  ```
  Жду: только новые файлы (backtest.py, history.py, smc_backtest.py, скилл,
  доки, тесты) + правки CLAUDE.md, .gitignore и test_multipair.py
  (заморозка часов в трёх тестах). **Ни одной строчки** в engine, journal,
  watcher, notifier и остальном боевом коде.

- [ ] **D2. Вотчер дышит** (не обязательно, локальный smoke):
  ```bash
  TELEGRAM_BOT_TOKEN=123:dummy TELEGRAM_CHAT_ID=1 python smc_watcher.py --once
  ```
  Жду: один цикл отработал и завершился; в выходной день — вердикты
  OFF_SESSION, это норма.

- [ ] **D3. После мержа в master**: Railway задеплоил, логи стартуют как
  обычно, 07:45-дайджест и алерты идут своим чередом. Бэктестер на проде
  не запускается вообще (его никто не импортирует).

---

## Что дальше

1. Блоки A и D1 — сегодня, им ничего не нужно.
2. Блок B — как только будешь за компом с интернетом; отчёты присылай мне.
3. Блок C — когда поставишь TradingView по инструкции.
4. Хочешь, чтобы отчёты гонялись прямо из облачных сессий — разреши
   `api.binance.com` и `api.twelvedata.com` в network policy окружения на
   claude.ai/code и добавь `TWELVEDATA_API_KEY` в переменные окружения —
   дальше блок B я буду прогонять сам.
