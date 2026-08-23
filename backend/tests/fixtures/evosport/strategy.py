from services.strategies.base import BaseStrategy


class FixtureStrategy(BaseStrategy):
    name = "Fixture no-op"
    description = "Returns no opportunities for EvoSport plumbing."

    def detect(self, events, markets, prices):
        return []
