"""D6: H4/H1 disagreement is labelled and denies the ⭐, never suppresses."""

from app.services.smc import sniper
from app.services.smc.engine import trends_disagree
from app.services.smc.models import Trend


class TestClassifyTrendGate:
    def _clean(self, **over):
        kwargs = dict(room=2.0, sweep="PDH", pd="ok", stale=False)
        kwargs.update(over)
        return sniper.classify(**kwargs)

    def test_star_when_trends_agree(self):
        assert self._clean(trend_disagrees=False).star is True

    def test_disagreement_denies_the_star(self):
        verdict = self._clean(trend_disagrees=True)
        assert verdict.star is False
        assert "trend" in verdict.missed

    def test_default_is_agreement(self):
        """Existing callers that pass four arguments keep today's behaviour."""
        assert sniper.classify(2.0, "PDH", "ok", False).star is True

    def test_disagreement_is_listed_alongside_other_misses(self):
        verdict = sniper.classify(
            room=0.1, sweep=None, pd="ok", stale=False, trend_disagrees=True
        )
        assert set(verdict.missed) >= {"room", "sweep", "trend"}


class TestTrendDisagreementDefinition:
    def test_h4_flat_is_not_disagreement(self):
        assert trends_disagree(Trend.FLAT, Trend.DOWN) is False

    def test_h1_flat_is_not_disagreement(self):
        assert trends_disagree(Trend.UP, Trend.FLAT) is False

    def test_opposite_trends_disagree(self):
        assert trends_disagree(Trend.UP, Trend.DOWN) is True
        assert trends_disagree(Trend.DOWN, Trend.UP) is True

    def test_same_trend_agrees(self):
        assert trends_disagree(Trend.UP, Trend.UP) is False

    def test_none_h1_is_not_disagreement(self):
        assert trends_disagree(Trend.UP, None) is False
