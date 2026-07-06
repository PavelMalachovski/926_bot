"""Triple Sync + Imbalance strategy engine (rules 0-8 orchestration)."""

from datetime import datetime, timezone
from typing import List, Optional

import structlog

from app.services.smc.data import BinanceDataFetcher
from app.services.smc.fvg import select_valid_fvg
from app.services.smc.models import (
    AnalysisResult,
    Candle,
    Direction,
    TradeSetup,
    Trend,
    Verdict,
)
from app.services.smc.sessions import active_session
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_zone,
    find_target_zone,
    last_protective_pivot,
    zone_touch_index,
)

logger = structlog.get_logger(__name__)

# Funding rate thresholds per 8h (Rule 9.3)
FUNDING_WARN = 0.0005  # 0.05%
FUNDING_DANGER = 0.001  # 0.1%


class TripleSyncEngine:
    """Runs one full strategy pass for a single symbol."""

    def __init__(
        self,
        symbol: str = "ETHUSDT",
        display_symbol: str = "ETHUSD",
        min_fvg_size: float = 2.0,
        sl_buffer: float = 2.0,
        min_rr: float = 2.0,
        risk_pct: float = 2.0,
        deposit: Optional[float] = None,
        enforce_sessions: bool = True,
        fetcher: Optional[BinanceDataFetcher] = None,
    ):
        self.symbol = symbol
        self.display_symbol = display_symbol
        self.min_fvg_size = min_fvg_size
        self.sl_buffer = sl_buffer
        self.min_rr = min_rr
        self.risk_pct = risk_pct
        self.deposit = deposit
        self.enforce_sessions = enforce_sessions
        self.fetcher = fetcher or BinanceDataFetcher(symbol)

    async def analyze(self) -> AnalysisResult:
        """Fetch fresh data and evaluate the full checklist."""
        now = datetime.now(tz=timezone.utc)
        result = AnalysisResult(
            symbol=self.display_symbol, verdict=Verdict.SKIP, checked_at=now
        )

        # Rule 0.1 — session filter (ETHUSD entries only inside session windows)
        result.session_name = active_session(now)
        if self.enforce_sessions and result.session_name is None:
            result.verdict = Verdict.OFF_SESSION
            result.reasons.append("Вне сессионных окон — входы по ETHUSD запрещены")
            return result

        data = await self.fetcher.fetch_all_timeframes()
        result.price = data["m5"][-1].close
        result.funding_rate = await self.fetcher.fetch_funding_rate()

        return self.evaluate(
            h4=data["h4"],
            h1=data["h1"],
            m5=data["m5"],
            result=result,
        )

    def evaluate(
        self,
        h4: List[Candle],
        h1: List[Candle],
        m5: List[Candle],
        result: AnalysisResult,
    ) -> AnalysisResult:
        """Evaluate rules 1-8 on the given candles (pure, testable)."""
        # Rule 1 — H4 global trend
        result.h4_trend = detect_trend(h4)
        if result.h4_trend == Trend.FLAT:
            result.verdict = Verdict.SKIP
            result.reasons.append("H4 во флете или CHoCH против тренда — нет направления")
            result.watch_notes.append(
                "Ждать чёткой структуры HH+HL или LH+LL на H4 (2 закрытых тела за экстремумом)"
            )
            return result

        direction = (
            Direction.LONG if result.h4_trend == Trend.UP else Direction.SHORT
        )

        # Rule 2 — H1 zone
        zone = find_h1_zone(h1, direction)
        if zone is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"H4 {'бычий' if direction == Direction.LONG else 'медвежий'}, "
                "но на H1 нет валидной непротестированной зоны "
                f"{'Demand' if direction == Direction.LONG else 'Supply'}"
            )
            result.watch_notes.append(
                "Ждать формирования свежей зоны на H1 (непротестированный "
                f"{'HL' if direction == Direction.LONG else 'LH'})"
            )
            return result
        result.h1_zone = zone

        # Rule 3 phase 1/2 — has price pulled back into the zone?
        touch = zone_touch_index(m5, zone)
        if touch is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"Цена ещё не дошла до зоны {'Demand' if zone.is_demand else 'Supply'} H1 "
                f"({zone.bottom:.2f}–{zone.top:.2f}) — фаза пуллбэка"
            )
            result.watch_notes.append(
                f"Алерт на {zone.top if zone.is_demand else zone.bottom:.2f} — "
                "при касании зоны проверить M5 на CHoCH + FVG"
            )
            result.watch_notes.append(
                f"Инвалидация: закрытие тела H1 "
                f"{'ниже ' + format(zone.bottom, '.2f') if zone.is_demand else 'выше ' + format(zone.top, '.2f')}"
            )
            return result

        # Zone still valid on M5? (body close through the far edge = invalidated)
        for c in m5[touch:]:
            if zone.is_demand and c.close < zone.bottom and c.body_low < zone.bottom:
                result.verdict = Verdict.SKIP
                result.reasons.append(
                    f"Цена закрылась ниже зоны Demand H1 ({zone.bottom:.2f}) — зона инвалидирована"
                )
                return result
            if not zone.is_demand and c.close > zone.top and c.body_high > zone.top:
                result.verdict = Verdict.SKIP
                result.reasons.append(
                    f"Цена закрылась выше зоны Supply H1 ({zone.top:.2f}) — зона инвалидирована"
                )
                return result

        # Rule 3 phase 2 — M5 CHoCH in trend direction inside the zone
        choch = find_choch(m5, direction, touch)
        if choch is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                "Цена в зоне H1, но M5 ещё не сформировал CHoCH в сторону тренда"
            )
            result.watch_notes.append(
                f"Ждать {'бычий' if direction == Direction.LONG else 'медвежий'} CHoCH на M5 "
                f"+ FVG ≥ ${self.min_fvg_size:.2f} внутри зоны"
            )
            return result

        # Rule 4 — valid FVG on/after the CHoCH. The imbalance belongs to the
        # impulse leg that breaks structure, so the 3-candle window may end on
        # the CHoCH candle itself (hence choch - 2), but never before the touch.
        fvg = select_valid_fvg(
            m5, direction, max(touch, choch - 2), self.min_fvg_size
        )
        if fvg is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"CHoCH на M5 есть, но валидного FVG нет (размер ≥ ${self.min_fvg_size:.2f}, "
                "заполнение < 50%, текущая сессия)"
            )
            result.watch_notes.append("Ждать формирования импульсного FVG на M5")
            return result

        # Rule 5 — entry level: proximal FVG boundary
        entry = fvg.top if direction == Direction.LONG else fvg.bottom

        # Rule 6 — stop loss behind the last confirmed M5 pivot + buffer
        pivot = last_protective_pivot(m5, direction, choch)
        if pivot is None:
            result.verdict = Verdict.SKIP
            result.reasons.append(
                "Нет подтверждённого экстремума M5 для стопа (2 закрытых тела) — SL не ставить"
            )
            return result
        if direction == Direction.LONG:
            stop_loss = pivot.price - self.sl_buffer
        else:
            stop_loss = pivot.price + self.sl_buffer

        risk = abs(entry - stop_loss)
        if risk <= 0:
            result.verdict = Verdict.SKIP
            result.reasons.append("Некорректная геометрия сделки: SL на уровне входа")
            return result

        # Rule 7 — TP at the nearest untested opposite zone (H1, fallback H4)
        target = find_target_zone(h1, direction, entry) or find_target_zone(
            h4, direction, entry
        )
        if target is None:
            result.verdict = Verdict.SKIP
            result.reasons.append(
                "Нет непротестированной противоположной зоны H1/H4 для тейк-профита"
            )
            return result
        take_profit = target.bottom if direction == Direction.LONG else target.top

        rr = abs(take_profit - entry) / risk
        if rr < self.min_rr:
            result.verdict = Verdict.SKIP
            result.reasons.append(
                f"RR 1:{rr:.1f} < минимума 1:{self.min_rr:.0f} — сделка математически невыгодна"
            )
            return result

        # Rule 8 — position size hint
        lot_hint = None
        if self.deposit:
            risk_usd = self.deposit * self.risk_pct / 100.0
            qty = risk_usd / risk
            lot_hint = (
                f"{qty:.4f} ETH (риск ${risk_usd:.2f} = {self.risk_pct:.1f}% "
                f"от депозита ${self.deposit:.0f})"
            )

        # Market entry allowed only if price is inside the FVG right now
        price = result.price or m5[-1].close
        entry_is_market = fvg.bottom <= price <= fvg.top

        result.setup = TradeSetup(
            direction=direction,
            entry=round(entry, 2),
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            rr=round(rr, 2),
            fvg=fvg,
            entry_is_market=entry_is_market,
            lot_hint=lot_hint,
        )
        result.verdict = (
            Verdict.APPROVED_MARKET if entry_is_market else Verdict.APPROVED_LIMIT
        )

        # Rule 9.3 — funding rate advisory
        if result.funding_rate is not None:
            rate = result.funding_rate
            if direction == Direction.LONG and rate > FUNDING_DANGER:
                result.funding_warning = (
                    f"Фандинг {rate * 100:.3f}%/8h > 0.1% — Long под повышенным риском "
                    "принудительного разворота. Рассмотри SKIP или уменьшение лота."
                )
            elif abs(rate) > FUNDING_WARN:
                result.funding_warning = (
                    f"Фандинг {rate * 100:.3f}%/8h в зоне 0.05–0.1% — вход на твоё усмотрение."
                )

        return result
