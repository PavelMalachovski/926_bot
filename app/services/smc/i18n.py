"""Bot-facing text in two languages (owner request 2026-09-10).

Russian is the default; English stays available and the owner switches
between them from the ⚙️ /settings menu. The choice is persisted in the
SQLite kv store (`WatcherState.language`) and applied process-wide through
`set_language` — the bot is one process serving one chat, so a module-level
current language is the whole state there is.

How it works:

- Every message string in the code is written in English and wrapped in
  `t("...")`. The English text IS the catalog key, so the code stays
  readable and a missing translation degrades to English instead of a
  KeyError.
- `RU` maps those keys to Russian. Placeholders are `str.format` fields
  (`{price}`, `{pair}` …); the caller passes them as keyword arguments to
  `t()` and the same names must appear in both languages — a test pins
  that so a typo in the catalog cannot raise at send time.
- Trading vocabulary stays Latin in both languages (owner decision
  2026-09-10): CHoCH, FVG, OB, LONG/SHORT, SL/TP, H4/H1/M5, Supply/Demand,
  PD, OTE, EQH/EQL, RR. Only prose, labels and buttons are translated.
- Logs stay English regardless of the language — `structlog` lines are for
  whoever reads Railway, not for the owner's chat.

`tests/conftest.py` pins the language to English for the whole suite, so
every existing assertion on message text keeps meaning what it says;
`test_i18n.py` exercises the Russian side explicitly.
"""

from typing import Dict, Optional

import structlog

logger = structlog.get_logger(__name__)

LANGUAGES = ("ru", "en")
DEFAULT_LANGUAGE = "ru"

LANGUAGE_NAMES = {"ru": "Русский", "en": "English"}
LANGUAGE_FLAGS = {"ru": "🇷🇺", "en": "🇬🇧"}

_current: Optional[str] = None


def normalize_language(value: Optional[str]) -> Optional[str]:
    """'RU' / 'ru-RU' / ' en ' -> 'ru' / 'ru' / 'en'; None for garbage."""
    if not value:
        return None
    code = str(value).strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGUAGES else None


def get_language() -> str:
    """The language messages are rendered in right now."""
    global _current
    if _current is None:
        _current = _configured_default()
    return _current


def set_language(value: Optional[str]) -> str:
    """Switch the process-wide language. Unknown values fall back to the
    configured default rather than raising: a poisoned kv row must never
    keep the watcher from starting."""
    global _current
    code = normalize_language(value)
    if code is None:
        if value:
            logger.warning("Unknown language, using default", value=value)
        code = _configured_default()
    _current = code
    return code


def _configured_default() -> str:
    try:
        from app.core.config import settings

        return normalize_language(settings.smc.language) or DEFAULT_LANGUAGE
    except Exception:  # config import failure must not take messages down
        return DEFAULT_LANGUAGE


def t(msgid: str, **kwargs) -> str:
    """Translate `msgid` into the current language and fill its fields.

    English is the key: when the current language is English, or the
    catalog has no entry, the key itself is returned (formatted). Every
    dynamic value is the CALLER's to escape — `t` never touches HTML.
    """
    text = msgid
    if get_language() == "ru":
        text = RU.get(msgid, msgid)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as e:
            # A catalog entry out of step with its key: log, fall back to
            # the English text so the message still reaches the owner.
            logger.error("i18n format failed", msgid=msgid, error=str(e))
            return msgid.format(**kwargs)
    return text


def tier_label(code: str) -> str:
    """Short name of a ⭐ condition (`sniper.classify` codes) for the
    'Missed for ⭐:' line. The codes themselves stay English in the data."""
    return t(_TIER.get(code, code))


_TIER = {
    "room": "room",
    "sweep": "sweep",
    "pd": "pd",
    "stale": "stale",
    "trend": "trend",
    "imbalance": "imbalance",
}


# --------------------------------------------------------------- catalog
#
# Grouped by the module the key lives in. Keep the placeholders identical
# to the key's (test_i18n pins it).

RU: Dict[str, str] = {}

# ---- shared vocabulary (notifier.py, engine.py, chart.py) ----
RU.update({
    "Demand": "Demand",
    "Supply": "Supply",
    "bullish": "бычий",
    "bearish": "медвежий",
    "uptrend": "аптренд",
    "downtrend": "даунтренд",
    "flat": "флэт",
    "swing high": "swing high",
    "swing low": "swing low",
    "market": "рынок",
    "range": "диапазон",
    "zone": "зона",
    "Zone": "Зона",
    "Price": "Цена",
    "Prague": "Прага",
    "Session": "Сессия",
    "Buy": "Buy",
    "Sell": "Sell",
    "{pips} pips": "{pips} пп",
    "discount": "дисконт",
    "premium": "премиум",
    "equilibrium": "равновесие",
    "not valid": "невалиден",
    "too small": "слишком мал",
    "over half filled": "заполнен больше чем наполовину",
    "closed through": "пробит закрытием",
    "from an earlier session": "из прошлой сессии",
    "1 touch": "1 касание",
    "{n} touches (few)": "{n} касания",
    "{n} touches": "{n} касаний",
    "untouched": "нетронут",
    "conditions not met": "условия не выполнены",
    # ⭐ conditions (sniper.classify codes) on the "Missed for ⭐" line
    "room": "запас хода",
    "sweep": "свип",
    "pd": "PD",
    "stale": "поздний вход",
    "trend": "тренд",
    "imbalance": "имбаланс",
    # AI read stances
    "agree": "согласен",
    "caution": "осторожно",
    "against": "против",
    "AI read": "Взгляд AI",
    "confidence {n}/5 · prefers {entry}": "уверенность {n}/5 · предпочитает {entry}",
})

# ---- notifier.py: the 🚨 setup card ----
RU.update({
    "SETUP READY — {pair} · {side}": "СЕТАП ГОТОВ — {pair} · {side}",
    "⚠️ H4 flat — direction from H1 {trend}": "⚠️ H4 флэт — направление от H1 ({trend})",
    "⚠️ against H4 — direction from the H1 {trend}": "⚠️ против H4 — направление от H1 ({trend})",
    "⚠️ H4 flat — direction from CHoCH (first leg, not with-trend)":
        "⚠️ H4 флэт — направление от CHoCH (первая нога, не по тренду)",
    "⚠️ H4/H1 flat — direction from the range boundary":
        "⚠️ H4/H1 флэт — направление от границы диапазона",
    " ⚠️ counter-hourly": " ⚠️ против H1",
    "🔹 Missed for ⭐: {missed}": "🔹 Не хватило для ⭐: {missed}",
    "   from this morning's plan": "   из утреннего плана",
    "   new zone — not in the plan": "   новая зона — в плане не было",
    "📦 Range box            {lo} – {hi}": "📦 Диапазон             {lo} – {hi}",
    "📍 Range LOW boundary  {lo} – {hi}": "📍 Нижняя граница       {lo} – {hi}",
    "📍 Range HIGH boundary  {lo} – {hi}": "📍 Верхняя граница      {lo} – {hi}",
    "📍 H1 {kind} zone ({zk})  {lo} – {hi}": "📍 H1 зона {kind} ({zk})  {lo} – {hi}",
    "⚡ M5 imbalance (FVG)   {lo} – {hi}   ← limit order ({entry})":
        "⚡ M5 имбаланс (FVG)    {lo} – {hi}   ← лимитный ордер ({entry})",
    "🧱 M5 order block       {entry}   ← limit order (no imbalance)":
        "🧱 M5 ордер-блок        {entry}   ← лимитный ордер (без имбаланса)",
    "📈 Market entry         {entry}   ← at the CHoCH (no imbalance)":
        "📈 Вход по рынку        {entry}   ← на CHoCH (без имбаланса)",
    "⚡ M5 imbalance         {lo} – {hi}   ✗ {flaw}":
        "⚡ M5 имбаланс          {lo} – {hi}   ✗ {flaw}",
    "⚡ M5 imbalance         none — the impulse left no gap":
        "⚡ M5 имбаланс          нет — импульс не оставил гэпа",
    "🧱 M5 order block       {lo} – {hi}   ← deeper entry ({entry})":
        "🧱 M5 ордер-блок        {lo} – {hi}   ← более глубокий вход ({entry})",
    "🛑 Stop reference       {extreme}   ← stop beyond it ({sl} with buffer)":
        "🛑 Опора для стопа      {extreme}   ← стоп за ней ({sl} с буфером)",
    "🛑 Swept liquidity      {extreme}   ← stop behind the wick ({sl} with buffer)":
        "🛑 Снятая ликвидность   {extreme}   ← стоп за фитилём ({sl} с буфером)",
    "🎯 Range HIGH target": "🎯 Цель — верх",
    "🎯 Range LOW target": "🎯 Цель — низ",
    "   ← full size, 1:{rr}": "   ← полным объёмом, 1:{rr}",
    "📈 Enter at market      {price}   ← SL {sl} · risk {risk}":
        "📈 Вход по рынку        {price}   ← SL {sl} · риск {risk}",
    "📈 Market entry         {price}   ✗ price is already beyond the stop — no market entry":
        "📈 Вход по рынку        {price}   ✗ цена уже за стопом — по рынку не входить",
    "🎯 no positive reward to the opposite boundary":
        "🎯 до противоположной границы нет положительной прибыли",
    "🎯 Range target         {tp}   ({rr})": "🎯 Цель диапазона       {tp}   ({rr})",
    "🎯 no unswept liquidity ahead": "🎯 впереди нет неснятой ликвидности",
    "🎯 Unswept liquidity ahead": "🎯 Неснятая ликвидность впереди",
    "      RR from {entry} / from OB": "      RR от {entry} / от OB",
    "      RR from {entry}": "      RR от {entry}",
    "     — none ahead": "     — впереди нет",
    "🧱 Untested zones further out   ← deeper entries":
        "🧱 Нетронутые зоны дальше   ← более глубокие входы",
    "   ▶️ price is inside the imbalance right now": "   ▶️ цена прямо сейчас внутри имбаланса",
    "   ▶️ price is inside the order block right now": "   ▶️ цена прямо сейчас внутри ордер-блока",
    "   ▶️ market entry — price is at the CHoCH": "   ▶️ вход по рынку — цена на CHoCH",
    "   ref · FVG {size}, {pct}% filled": "   справка · FVG {size}, заполнен на {pct}%",
    "   ref · FVG {size}, {pct}% filled — {flaw}": "   справка · FVG {size}, заполнен на {pct}% — {flaw}",
    "   ref · no M5 imbalance in the impulse": "   справка · в импульсе нет M5 имбаланса",
    "   ref · tracked objective {tp} (1:{rr}) · {target}":
        "   справка · отслеживаемая цель {tp} (1:{rr}) · {target}",
    "   ref · size {size}": "   справка · объём {size}",
    "   ref · funding {rate}%/8h": "   справка · фандинг {rate}%/8ч",
    "   ref · aggressive profile — first-leg entry": "   справка · агрессивный профиль — вход на первой ноге",
    "   ref · a pending order expires with this session (Rule 10)":
        "   справка · отложенный ордер живёт до конца сессии (правило 10)",
    "   ref · {when} Prague": "   справка · {when} Прага",
    " · price {price}": " · цена {price}",
    "✅ Took it": "✅ Взял",
    "❌ Skipped": "❌ Пропустил",
})

# ---- notifier.py: quiet lines, heartbeat, watch screen ----
RU.update({
    "🔹 <b>{pair} {side}</b> · range {lo}–{hi} · entry {entry} · SL {sl} · {target}":
        "🔹 <b>{pair} {side}</b> · диапазон {lo}–{hi} · вход {entry} · SL {sl} · {target}",
    "🔹 <b>{pair} {side}</b> · entry {entry} · SL {sl} · TP1 {tp1} · runner {runner}":
        "🔹 <b>{pair} {side}</b> · вход {entry} · SL {sl} · TP1 {tp1} · раннер {runner}",
    "Missed for ⭐: {missed}{pd} · {when} Prague": "Не хватило для ⭐: {missed}{pd} · {when} Прага",
    "😴 {pair} {hhmm} — off session, entries are not allowed. Will check again on schedule.":
        "😴 {pair} {hhmm} — вне сессии, входы запрещены. Проверю снова по расписанию.",
    "🔍 {pair} {hhmm} — no setup. {reason}.": "🔍 {pair} {hhmm} — сетапа нет. {reason}.",
    "⏳ {pair} {hhmm} — the setup reported earlier is still active. Nothing new.":
        "⏳ {pair} {hhmm} — ранее найденный сетап всё ещё активен. Ничего нового.",
    "⚡ <b>Aggressive profile</b> — first-leg entry, lower-probability":
        "⚡ <b>Агрессивный профиль</b> — вход на первой ноге, вероятность ниже",
    "H4 bias": "Направление H4",
    "Range box": "Диапазон",
    "Range LOW boundary": "Нижняя граница диапазона",
    "Range HIGH boundary": "Верхняя граница диапазона",
    "H1 zone": "Зона H1",
    "No setup yet (Setup Watch)": "Сетапа пока нет (наблюдение)",
    "What is needed for an entry": "Что нужно для входа",
    "Verdict": "Вердикт",
})

# ---- notifier.py: PD radar, audit, plan, zone alert ----
RU.update({
    " · bias {side}": " · направление {side}",
    "{pct}% of the range": "{pct}% диапазона",
    "   ⭐ price is inside": "   ⭐ цена внутри",
    "📍 H1 {kind} zone      {lo} – {hi}": "📍 H1 зона {kind}      {lo} – {hi}",
    "🎯 Liquidity ahead   {target}": "🎯 Ликвидность впереди   {target}",
    "Watching M5 for a {bias} CHoCH + FVG.": "Жду на M5 {bias} CHoCH + FVG.",
    "Where": "Где",
    "Entry": "Вход",
    "Risk": "Риск",
    "no unswept liquidity ahead": "впереди нет неснятой ликвидности",
    "Strategy audit — {pair}": "Аудит по стратегии — {pair}",
    "M5 close {hhmm} Prague": "закрытие M5 {hhmm} Прага",
    "📦 Range box {lo}–{hi}": "📦 Диапазон {lo}–{hi}",
    "🚨 <b>Setup formed</b> — market entry {entry} · SL {sl} · risk {risk}":
        "🚨 <b>Сетап сформирован</b> — вход по рынку {entry} · SL {sl} · риск {risk}",
    "(off session) ": "(вне сессии) ",
    "Pending (limit) entries": "Отложенные (лимитные) входы",
    "🎯 one target each — the opposite boundary, full size (D14)":
        "🎯 у каждого одна цель — противоположная граница, полным объёмом (D14)",
    "⚠️ A pending order lives only within its session (Rule 10); once the setup forms, "
    "the 🚨 alert re-anchors the SL to the swept extreme (Rule 6).":
        "⚠️ Отложенный ордер живёт только в своей сессии (правило 10); когда сетап "
        "сформируется, 🚨 алерт переставит SL за снятый экстремум (правило 6).",
    "→ No pending entry to place: {note}": "→ Отложенный вход ставить негде: {note}",
    "nothing to wait at": "ждать негде",
    "Pre-Market Plan (H4 {trend})": "Премаркет-план (H4 {trend})",
    "Pre-Market Plan": "Премаркет-план",
    "Live now": "Сейчас",
    " (speculative)": " (спекулятивно)",
    " plan": " план",
    "   📐 RR ~1:{rr} (approx)": "   📐 RR ~1:{rr} (приблизительно)",
    "   Trigger: M5 {bias} CHoCH + FVG inside the zone": "   Триггер: {bias} CHoCH + FVG на M5 внутри зоны",
    "   ⚠️ this boundary has already been swept once — liquidity may be thinner here":
        "   ⚠️ эту границу уже снимали — ликвидности за ней может быть меньше",
    "range boundary": "границей диапазона",
    "⚠️ SL is preliminary (beyond the {anchor}); the live 🚨 alert re-anchors it to the swept "
    "extreme and it may be wider. Order lives only within its session.":
        "⚠️ SL предварительный (за {anchor}); живой 🚨 алерт переставит его за снятый "
        "экстремум, и он может стать шире. Ордер живёт только в своей сессии.",
    "— press a pair for its strategy audit (pending entries)":
        "— нажми пару, чтобы получить аудит по стратегии (отложенные входы)",
    "upd {hhmm}": "обн. {hhmm}",
    "market closed": "рынок закрыт",
    "waiting": "ожидание",
    "no plan": "плана нет",
    "🌐 All pairs": "🌐 Все пары",
    "🔔 <b>{pair}</b>: price is at the range {edge} {price}":
        "🔔 <b>{pair}</b>: цена на границе диапазона {edge} {price}",
    "📋 Plan: {side} — target the range {edge} {tp} | 🛑 SL {sl} | ~1:{rr}{spec}":
        "📋 План: {side} — цель граница {edge} {tp} | 🛑 SL {sl} | ~1:{rr}{spec}",
    "🔔 <b>{pair}</b>: price reached the {kind} zone {lo}–{hi}":
        "🔔 <b>{pair}</b>: цена дошла до зоны {kind} {lo}–{hi}",
    "📋 Plan: {side} — {order} Limit {entry} | 🛑 SL {sl} | 🎯 TP {tp} | ~1:{rr}{spec}":
        "📋 План: {side} — {order} Limit {entry} | 🛑 SL {sl} | 🎯 TP {tp} | ~1:{rr}{spec}",
    "🔕 Mute {pair} zone alerts till {hhmm}": "🔕 Не тревожить по зонам {pair} до {hhmm}",
})

# ---- engine.py: checklist reasons, watch notes, warnings ----
RU.update({
    "H1 Demand zone": "зоны H1 Demand",
    "H1 Supply zone": "зоны H1 Supply",
    "range LOW boundary": "нижней границы диапазона",
    "range HIGH boundary": "верхней границы диапазона",
    ", Mon-Fri": ", пн–пт",
    "Outside trading hours (08:00-18:30 Prague{weekdays}) — no entries for {pair}":
        "Вне торговых часов (08:00-18:30 Прага{weekdays}) — по {pair} не входим",
    "Market closed — last M5 candle {minutes} min ago":
        "Рынок закрыт — последняя свеча M5 была {minutes} мин назад",
    "H4 and H1 are both flat inside a range ({lo}–{hi}), but price sits mid-range "
    "— no boundary to trade from yet":
        "H4 и H1 во флэте внутри диапазона ({lo}–{hi}), но цена посередине "
        "— торговать пока не от чего",
    "Set alerts at {hi} (short) and {lo} (long) — on a boundary touch, check M5 "
    "for a CHoCH + FVG":
        "Поставь алерты на {hi} (шорт) и {lo} (лонг) — при касании границы "
        "смотри на M5 CHoCH + FVG",
    "H4 is flat or CHoCH against the trend — no direction":
        "H4 во флэте или CHoCH против тренда — направления нет",
    "Wait for a clear HH+HL or LH+LL structure on H4 (2 closed bodies beyond the extreme)":
        "Жди чёткой структуры HH+HL или LH+LL на H4 (2 закрытых тела за экстремумом)",
    "H4 is {bias}, but H1 has no valid untested {kind} zone (no order block, no "
    "untouched H1 imbalance)":
        "H4 {bias}, но на H1 нет валидной нетронутой зоны {kind} (ни ордер-блока, "
        "ни нетронутого имбаланса H1)",
    "Wait for a fresh H1 zone to form — an untested {pivot} order block or an "
    "untouched H1 imbalance":
        "Жди формирования свежей зоны H1 — нетронутого ордер-блока {pivot} или "
        "нетронутого имбаланса H1",
    "Price has not reached the {zone} ({lo}–{hi}) yet — pullback phase":
        "Цена ещё не дошла до {zone} ({lo}–{hi}) — фаза отката",
    "Set an alert at {edge} — on zone touch, check M5 for a CHoCH + FVG":
        "Поставь алерт на {edge} — при касании зоны смотри на M5 CHoCH + FVG",
    "below": "ниже",
    "above": "выше",
    "below {level}": "ниже {level}",
    "above {level}": "выше {level}",
    "Invalidation: a close {beyond} that still holds":
        "Инвалидация: закрытие {beyond}, которое удержалось",
    "Invalidation: H1 body close {beyond}": "Инвалидация: закрытие тела H1 {beyond}",
    "Price closed {below_above} the {zone} ({level}) — invalidated":
        "Цена закрылась {below_above} {zone} ({level}) — инвалидирована",
    "Price is at the {zone}, but M5 has not printed a CHoCH in the trade direction yet":
        "Цена у {zone}, но M5 ещё не напечатал CHoCH в сторону сделки",
    "Price is in the H1 zone, but M5 has not printed a CHoCH in the trend direction yet":
        "Цена в зоне H1, но M5 ещё не напечатал CHoCH по тренду",
    "Wait for a {bias} M5 CHoCH + FVG ≥ {size} inside the zone":
        "Жди {bias} CHoCH на M5 + FVG ≥ {size} внутри зоны",
    "M5 CHoCH is there, but no valid FVG — ": "CHoCH на M5 есть, но валидного FVG нет — ",
    "Wait for an impulse FVG to form on M5": "Жди импульсного FVG на M5",
    "no FVG has formed in the impulse yet": "в импульсе FVG ещё не сформировался",
    "best candidate: ": "лучший кандидат: ",
    "size {size} < required {required}": "размер {size} < нужных {required}",
    "invalidated (body closed through the gap)": "инвалидирован (тело закрылось сквозь гэп)",
    "{pct}% filled (max 50%)": "заполнен на {pct}% (макс. 50%)",
    "formed in a previous session": "сформирован в прошлой сессии",
    "Invalid trade geometry: SL at the entry level":
        "Некорректная геометрия сделки: SL на уровне входа",
    "Invalid trade geometry: entry is on the wrong side of the SL":
        "Некорректная геометрия сделки: вход не с той стороны от SL",
    "price has run {r}R past the imbalance": "цена ушла на {r}R от имбаланса",
    "the opposite boundary sits inside the stop buffer":
        "противоположная граница внутри буфера стопа",
    "RR to the opposite boundary is 1:{rr}": "RR до противоположной границы 1:{rr}",
    "no unswept liquidity ahead": "впереди нет неснятой ликвидности",
    "nearest liquidity sits inside the stop buffer":
        "ближайшая ликвидность внутри буфера стопа",
    "RR to the nearest liquidity is 1:{rr}": "RR до ближайшей ликвидности 1:{rr}",
    "Funding {rate}%/8h > {danger}% — longs are at elevated squeeze risk. Consider "
    "SKIP or a smaller size.":
        "Фандинг {rate}%/8ч > {danger}% — у лонгов повышенный риск сквиза. "
        "Подумай о пропуске или меньшем объёме.",
    "Funding {rate}%/8h < -{danger}% — shorts are at elevated squeeze risk. Consider "
    "SKIP or a smaller size.":
        "Фандинг {rate}%/8ч < -{danger}% — у шортов повышенный риск сквиза. "
        "Подумай о пропуске или меньшем объёме.",
    "Funding {rate}%/8h is above the {warn}% advisory level — your call.":
        "Фандинг {rate}%/8ч выше рекомендательного уровня {warn}% — решай сам.",
})

# ---- plan.py / pending.py ----
RU.update({
    "H1 {kind} zone ({zk}) {lo}-{hi}": "зоны H1 {kind} ({zk}) {lo}-{hi}",
    "Price is already inside the {zone} — no pullback left to project, the live "
    "checklist takes over":
        "Цена уже внутри {zone} — откат проецировать нечего, дальше работает "
        "живой чеклист",
    "Price is below the {zone} — wait for a fresh untested HL to form under price":
        "Цена ниже {zone} — жди формирования свежего нетронутого HL под ценой",
    "Price is above the {zone} — wait for a fresh untested LH to form above price":
        "Цена выше {zone} — жди формирования свежего нетронутого LH над ценой",
    "{zone} is live, but entry and stop coincide — no risk to measure":
        "Для {zone}: вход и стоп совпадают — риск не измерить",
    "{zone} is live, but there is no unswept liquidity ahead of it to aim at":
        "Для {zone}: впереди нет неснятой ликвидности, куда целиться",
    "Zone {kind} {lo}-{hi} is live, but the nearest liquidity sits inside the SL "
    "buffer — no positive reward":
        "Зона {kind} {lo}-{hi} активна, но ближайшая ликвидность внутри буфера SL "
        "— положительной прибыли нет",
    "Zone {kind} {lo}-{hi} is live, but the nearest liquidity gives 1:{rr} — waiting "
    "for other structure":
        "Зона {kind} {lo}-{hi} активна, но ближайшая ликвидность даёт 1:{rr} — жду "
        "другой структуры",
    "Range boundary is live, but entry and stop coincide — no risk to measure":
        "Граница диапазона активна, но вход и стоп совпадают — риск не измерить",
    "Range boundary is live, but the opposite boundary sits inside the SL buffer — "
    "no positive reward":
        "Граница диапазона активна, но противоположная граница внутри буфера SL — "
        "положительной прибыли нет",
    "Range boundary is live, but the target gives 1:{rr} — waiting for other structure":
        "Граница диапазона активна, но цель даёт 1:{rr} — жду другой структуры",
    "Market closed (weekend) — no plan": "Рынок закрыт (выходные) — плана нет",
    "H4 flat — direction from H1 uptrend": "H4 флэт — направление от аптренда H1",
    "H4 flat — direction from H1 downtrend": "H4 флэт — направление от даунтренда H1",
    "H4 and H1 are both flat — trading the range boundaries":
        "H4 и H1 во флэте — торгуем границы диапазона",
    "H4 flat — direction from CHoCH (first leg, not with-trend)":
        "H4 флэт — направление от CHoCH (первая нога, не по тренду)",
    "Range LOW": "Низ диап.",
    "Range HIGH": "Верх диап.",
    "M5 FVG edge": "Край M5 FVG",
    " · next": " · след.",
    "price has run past every limit rung of this setup — the market entry is what is left":
        "цена ушла за все лимитные уровни этого сетапа — остался только вход по рынку",
    "no direction": "направления нет",
})

# ---- chart.py (PNG labels) ----
RU.update({
    "ENTRY": "ВХОД",
    "RANGE HIGH": "ВЕРХ ДИАПАЗОНА",
    "RANGE LOW": "НИЗ ДИАПАЗОНА",
    "(alt)": "(альт.)",
    "no liquidity ahead": "впереди нет ликвидности",
    "{pair} M5 — {side} setup | {rr} | {when} Prague":
        "{pair} M5 — сетап {side} | {rr} | {when} Прага",
    "{pair} H1 — Pre-Market Plan | price {price}": "{pair} H1 — премаркет-план | цена {price}",
})

# ---- news.py (07:55 digest) ----
RU.update({
    "(Prague time)": "(время Праги)",
    "⚠️ Calendar not loaded yet": "⚠️ Календарь ещё не загружен",
    "✅ No red news for your pairs ({pairs}) today. Clean hunting.":
        "✅ Сегодня красных новостей по твоим парам ({pairs}) нет. Чистая охота.",
    "    ⛔ no entries {start}–{end}": "    ⛔ без входов {start}–{end}",
    "London": "Лондон",
    "New York": "Нью-Йорк",
    "✅ clear": "✅ чисто",
    "Outside trading hours": "Вне торговых часов",
    "⛔ Blackout rule: {before} min before / {after} min after each release.":
        "⛔ Правило блэкаута: {before} мин до / {after} мин после каждого релиза.",
})

# ---- trade_journal.py (/journal) ----
RU.update({
    "🔍 No trades could be recognized in the screenshot.\nTry sending a clearer "
    "screenshot of the MT4 history.":
        "🔍 На скриншоте не удалось распознать ни одной сделки.\nПопробуй прислать "
        "более чёткий скриншот истории MT4.",
    "Recognized trades: {n}": "Распознано сделок: {n}",
    "Batch total: {total}": "Итого по пакету: {total}",
    "Save these trades to the journal?": "Сохранить эти сделки в журнал?",
    "📓 <b>Trade journal is empty</b>\n\nSend a screenshot of your MetaTrader history — "
    "I'll recognize the trades and save them here.":
        "📓 <b>Журнал сделок пуст</b>\n\nПришли скриншот истории MetaTrader — "
        "я распознаю сделки и сохраню их здесь.",
    "Trade journal": "Журнал сделок",
    "Total P/L": "Общий P/L",
    "Total trades": "Всего сделок",
    "Winners": "Прибыльных",
    "Losers": "Убыточных",
    "Win rate": "Винрейт",
    "Profit factor": "Профит-фактор",
    "Best": "Лучшая",
    "Worst": "Худшая",
    "By symbol": "По инструментам",
    "({n} trades, WR {wr}%)": "({n} сделок, WR {wr}%)",
    "Recent trades": "Последние сделки",
})

# ---- smc_watcher.py: live card, discipline, warnings ----
RU.update({
    "📈 Filled @ {entry}{when}": "📈 Исполнен @ {entry}{when}",
    "🎯 <b>TP HIT</b>{when} — planned +{rr}R": "🎯 <b>TP ВЗЯТ</b>{when} — по плану +{rr}R",
    "🛑 <b>SL HIT</b>{when} — −1R": "🛑 <b>SL ВЗЯТ</b>{when} — −1R",
    "🎯 <b>TP1 → BE</b>{when}": "🎯 <b>TP1 → БУ</b>{when}",
    "🏆 <b>RUNNER HIT</b>{when}": "🏆 <b>РАННЕР ВЗЯТ</b>{when}",
    "🗑 Expired unfilled — order dies with its session (Rule 10)":
        "🗑 Истёк без исполнения — ордер умирает вместе с сессией (правило 10)",
    "⌛ Timed out — untracked after 5 days": "⌛ Таймаут — через 5 дней не отслеживается",
    "⏳ Position live — tracking TP/SL": "⏳ Позиция открыта — слежу за TP/SL",
    "⏳ Position live — tracking SL (no objective recorded)":
        "⏳ Позиция открыта — слежу за SL (цель не записана)",
    "❌ RULE 9: EURUSD and GBPUSD in the same direction — forbidden combination "
    "(correlation ~0.90). Pick ONE of the pairs.":
        "❌ ПРАВИЛО 9: EURUSD и GBPUSD в одну сторону — запрещённая комбинация "
        "(корреляция ~0.90). Выбери ОДНУ пару.",
    "❌ RULE 9: {pair} {side} + USDJPY {jpy_side} — a triple bet on one side of USD. "
    "Forbidden.":
        "❌ ПРАВИЛО 9: {pair} {side} + USDJPY {jpy_side} — тройная ставка на одну "
        "сторону USD. Запрещено.",
    " Check your API key (it may have expired).": " Проверь API-ключ (возможно, истёк).",
    "⚠️ <b>{pair}</b>: data source failed — {detail}.{hint}":
        "⚠️ <b>{pair}</b>: источник данных недоступен — {detail}.{hint}",
    "Signal not found (journal may have been reset)":
        "Сигнал не найден (журнал мог быть сброшен)",
    "{pair} marked as skipped": "{pair} отмечен как пропущенный",
    "{pair} marked as taken — tracking your stats; muted for {hours}h":
        "{pair} отмечен как взятый — веду статистику; пара замолчит на {hours}ч",
    "🛑 <b>RULE 0.2:</b> two taken stop-losses today — the trading day is CLOSED. "
    "No more alerts until tomorrow. A skipped bad day is a win.":
        "🛑 <b>ПРАВИЛО 0.2:</b> два взятых стопа за день — торговый день ЗАКРЫТ. "
        "Алертов до завтра не будет. Пропущенный плохой день — это победа.",
    "⚠️ <b>RULE 0.4:</b> 🔴 {title} ({currency}) in {minutes} min ({hhmm} Prague)":
        "⚠️ <b>ПРАВИЛО 0.4:</b> 🔴 {title} ({currency}) через {minutes} мин "
        "({hhmm} Прага)",
    "• {pair} — {position} — {action}!": "• {pair} — {position} — {action}!",
    "move the SL to breakeven": "переставь SL в безубыток",
    "cancel the pending order": "сними отложенный ордер",
    "an open position": "открытая позиция",
    "an active limit order": "активный лимитный ордер",
    "😴 Market closed — computed on the last closed candles.":
        "😴 Рынок закрыт — посчитано по последним закрытым свечам.",
    "Plan updated — {pair}": "План обновлён — {pair}",
    "Full plan: press {pair} on today's summary.":
        "Полный план: нажми {pair} под сегодняшней сводкой.",
    "🚨 LIVE SETUP NOW — {side} entry {entry}, SL {sl}{tail}":
        "🚨 СЕТАП ПРЯМО СЕЙЧАС — {side} вход {entry}, SL {sl}{tail}",
    ", no TP (no structural objective)": ", без TP (структурной цели нет)",
})

# ---- telegram_bot.py: commands, menu, /settings ----
RU.update({
    "<b>SMC Watcher</b> — Triple Sync + Imbalance\n\n"
    "I check the selected pairs every 5 minutes during sessions and send "
    "exactly two things:\n"
    "📰 the red-news digest at 07:55 Prague\n"
    "🚨 a setup alert when a setup has formed — enter at market — with "
    "Claude's read appended to the card\n\n"
    "<b>Commands:</b>\n"
    "/plan — strategy audit for a pair: pending (limit) entries + Claude's "
    "read, on fresh candles\n"
    "/journal — trade journal: send an MT4 history screenshot to log trades\n"
    "/news — today's red news (Forex Factory)\n"
    "/settings — language, pairs, alert level, pause\n"
    "/help — this help":
        "<b>SMC Watcher</b> — Triple Sync + Imbalance\n\n"
        "Я проверяю выбранные пары каждые 5 минут в сессии и присылаю ровно "
        "две вещи:\n"
        "📰 дайджест красных новостей в 07:55 по Праге\n"
        "🚨 алерт, когда сетап сформирован — вход по рынку — с разбором "
        "Claude под карточкой\n\n"
        "<b>Команды:</b>\n"
        "/plan — аудит по стратегии для пары: отложенные (лимитные) входы + "
        "разбор Claude, на свежих свечах\n"
        "/journal — журнал сделок: пришли скриншот истории MT4, чтобы записать сделки\n"
        "/news — красные новости на сегодня (Forex Factory)\n"
        "/settings — язык, пары, уровень алертов, пауза\n"
        "/help — эта справка",
    "Strategy audit for a pair + Claude's read": "Аудит по стратегии для пары + разбор Claude",
    "Trade journal from MT4 screenshots": "Журнал сделок по скриншотам MT4",
    "Today's red news (Forex Factory)": "Красные новости на сегодня (Forex Factory)",
    "Language, pairs, alert level, pause": "Язык, пары, уровень алертов, пауза",
    "What this bot does": "Что делает этот бот",
    "SMC Triple Sync + Imbalance setup alerts": "Алерты сетапов SMC Triple Sync + Imbalance",
    "Watches ETHUSD and forex pairs for Triple Sync + Imbalance setups (H4 trend → H1 "
    "zone → M5 CHoCH + FVG) and sends an urgent alert with entry/SL/TP when everything "
    "lines up. Trading hours 08:00-18:30 Prague.":
        "Следит за ETHUSD и форекс-парами в поисках сетапов Triple Sync + Imbalance "
        "(тренд H4 → зона H1 → M5 CHoCH + FVG) и присылает срочный алерт со входом, "
        "SL и TP, когда всё сошлось. Торговые часы 08:00-18:30 по Праге.",
    "Signals per pair — tap to pause (☐) or resume (✅):":
        "Сигналы по парам — нажми, чтобы выключить (☐) или включить (✅):",
    "⏸ <b>Paused</b> — no alerts or messages until you resume.":
        "⏸ <b>Пауза</b> — ни алертов, ни сообщений, пока не возобновишь.",
    "▶️ Resume": "▶️ Возобновить",
    "▶️ Resumed": "▶️ Возобновлено",
    "▶️ <b>Resumed</b> — watching pairs again.": "▶️ <b>Возобновлено</b> — снова слежу за парами.",
    "Resumed": "Возобновлено",
    "Paused": "Пауза",
    "⏸ Pause": "⏸ Пауза",
    "Trade journal is not available.": "Журнал сделок недоступен.",
    "News filter is not available.": "Фильтр новостей недоступен.",
    "No pairs enabled — turn one on in /settings first.":
        "Нет включённых пар — сначала включи хотя бы одну в /settings.",
    "🔬 Strategy audit — choose a pair (fresh candles + Claude's read):":
        "🔬 Аудит по стратегии — выбери пару (свежие свечи + разбор Claude):",
    "Unknown command. /help for the list.": "Неизвестная команда. Список — /help.",
    "⚠️ Recognition unavailable: ANTHROPIC_API_KEY is not configured.":
        "⚠️ Распознавание недоступно: не задан ANTHROPIC_API_KEY.",
    "🔍 Recognizing trades from the screenshot, one moment...":
        "🔍 Распознаю сделки на скриншоте, секунду...",
    "❌ Could not download the image. Please try again.":
        "❌ Не удалось скачать картинку. Попробуй ещё раз.",
    "💾 Save": "💾 Сохранить",
    "❌ Cancel": "❌ Отмена",
    "❌ Error while recognizing the screenshot. Please send a clearer image.":
        "❌ Ошибка при распознавании скриншота. Пришли картинку почётче.",
    "✅ Taken — tracked in the journal": "✅ Взял — отслеживаю в журнале",
    "{pair}: that alert's session block already ended — nothing muted":
        "{pair}: сессионный блок этого алерта уже закончился — ничего не заглушено",
    "🔕 Block already ended": "🔕 Блок уже закончился",
    "{pair} zone alerts muted till {hhmm}": "{pair}: алерты по зонам заглушены до {hhmm}",
    "🔕 Muted till {hhmm}": "🔕 Тихо до {hhmm}",
    "Sending all audits…": "Отправляю все аудиты…",
    "Sending {pair} audit…": "Отправляю аудит {pair}…",
    "Building {pair} plan…": "Строю план {pair}…",
    "Unknown pair {pair}": "Неизвестная пара {pair}",
    "{pair}: ✅ enabled": "{pair}: ✅ включена",
    "{pair}: ⛔ disabled": "{pair}: ⛔ выключена",
    "⚠️ Nothing to save (batch not found or already processed).":
        "⚠️ Сохранять нечего (пакет не найден или уже обработан).",
    "⚠️ Empty": "⚠️ Пусто",
    "✅ Saved trades: {n}": "✅ Сохранено сделок: {n}",
    "♻️ Skipped duplicates: {n}": "♻️ Пропущено дубликатов: {n}",
    "💾 Saved ({n})": "💾 Сохранено ({n})",
    "Done": "Готово",
    "❌ Cancelled. Trades were not saved (removed: {n}).":
        "❌ Отменено. Сделки не сохранены (удалено: {n}).",
    "❌ Cancelled": "❌ Отменено",
    "Cancelled": "Отменено",
    "Error while processing": "Ошибка при обработке",
    # /settings
    "Settings": "Настройки",
    "Language": "Язык",
    "Pairs": "Пары",
    "Setup alerts": "Алерты сетапов",
    "Status": "Статус",
    "paused": "на паузе",
    "active": "активен",
    "none": "нет",
    "all setups": "все сетапы",
    "⭐ only": "только ⭐",
    "no setup alerts": "без алертов сетапов",
    "🌐 Language": "🌐 Язык",
    "📊 Pairs": "📊 Пары",
    "🔔 Alerts": "🔔 Алерты",
    "« Back": "« Назад",
    "Unknown language": "Неизвестный язык",
    "Language: {name}": "Язык: {name}",
    "tap to pause (☐) or resume (✅)": "нажми, чтобы выключить (☐) или включить (✅)",
    "a ⭐ always goes through; regular setups are still journal-recorded when not sent":
        "⭐ проходит всегда; обычные сетапы всё равно пишутся в журнал, даже если не отправлены",
    "Setup alerts: {level}": "Алерты сетапов: {level}",
    "Unknown alert level": "Неизвестный уровень алертов",
})

# ---- the primary plan on the 🚨 card (owner decision 2026-09-10) ----
RU.update({
    "📋 Per the {when} plan: {side}": "📋 По плану {when}: {side}",
    "📋 Not the {when} plan ({side} {zone} there)": "📋 Не по плану {when} (там {side} {zone})",
    "preferred {entry}": "предпочитал {entry}",
    "no zone": "без зоны",
})

# ---- /plan audit: AI status + distance to the MAIN entry (2026-09-10) ----
RU.update({
    "🧠 AI read is off (SMC_AI_READ=false)": "🧠 Разбор AI выключен (SMC_AI_READ=false)",
    "🧠 AI read is off: ANTHROPIC_API_KEY is not set": "🧠 Разбор AI выключен: не задан ANTHROPIC_API_KEY",
    "🧠 AI read failed: {reason}": "🧠 Разбор AI не получен: {reason}",
    "no answer": "нет ответа",
    "📏 To the MAIN entry {entry}: {distance} ({pct}%)": "📏 До входа MAIN {entry}: {distance} ({pct}%)",
})

# ---- plan cancelled (owner request 2026-09-10) ----
RU.update({
    "📋 <b>{pair} plan cancelled</b> — the H1 {kind} zone {lo}–{hi} was broken by a close "
    "at {close} ({hhmm} Prague). Pull the limit if you placed one; press /plan for a "
    "fresh read.":
        "📋 <b>План {pair} отменён</b> — зона H1 {kind} {lo}–{hi} пробита закрытием "
        "{close} ({hhmm} Прага). Сними лимитку, если ставил; нажми /plan для нового разбора.",
})

# ---- audit polish (2026-09-10): size row, session clock, Claude accuracy ----
RU.update({
    "Size": "Объём",
    "⏱ {session} ends in {left} ({hhmm} Prague) — a pending order placed now expires then":
        "⏱ Сессия {session} закончится через {left} ({hhmm} Прага) — отложка, поставленная "
        "сейчас, истечёт тогда же",
    "Claude vs outcomes — last {days} days": "Claude против исходов — последние {days} дней",
    "no resolved alerts with a read yet": "закрытых алертов с разбором пока нет",
    "{stance}: {n} · {wins} wins / {losses} stops ({rate}%)":
        "{stance}: {n} · {wins} в плюс / {losses} стопов ({rate}%)",
})
