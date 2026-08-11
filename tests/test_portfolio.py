"""Position lifecycle: open, mark, exit, account, learn, recover.

This is the integration test. It runs the real Portfolio, the real exit rules
and the real learning layer against a controllable executor, so a break
anywhere along that path shows up here.
"""

from __future__ import annotations

import pytest

from mctb.brain.edge import PersonalEdge
from mctb.brain.executor import Executor
from mctb.brain.portfolio import Portfolio
from mctb.config import Config
from mctb.db import Database
from mctb.models import Candidate, Fill, PositionState, ScoreBreakdown


class FakeExecutor(Executor):
    """Executor with a settable price and controllable failures."""

    paper = True

    def __init__(self, price: float = 1.0e-7, bankroll: float = 1.0) -> None:
        self.price = price
        self._bankroll = bankroll
        self.buys: list[tuple[str, float]] = []
        self.sells: list[tuple[str, float, str]] = []
        self.fail_buys = False
        self.fail_sells = False
        self.sell_failures_remaining = 0

    async def bankroll(self) -> float:
        return self._bankroll

    async def buy(self, mint: str, sol_amount: float, symbol: str = "") -> Fill:
        self.buys.append((mint, sol_amount))
        if self.fail_buys:
            return Fill(mint=mint, symbol=symbol, side="buy", ok=False, error="simulated failure")
        tokens = sol_amount / self.price
        self._bankroll -= sol_amount
        return Fill(
            mint=mint, symbol=symbol, side="buy", ok=True,
            sol_amount=sol_amount, token_amount=tokens,
            price_sol=self.price, fee_sol=0.0005,
        )

    async def sell(self, mint, token_amount, symbol="", reason="", urgent=False) -> Fill:
        self.sells.append((mint, token_amount, reason))
        if self.fail_sells or self.sell_failures_remaining > 0:
            self.sell_failures_remaining = max(0, self.sell_failures_remaining - 1)
            return Fill(
                mint=mint, symbol=symbol, side="sell", ok=False,
                error="simulated exit failure", exit_reason=reason,
            )
        proceeds = token_amount * self.price
        self._bankroll += proceeds
        return Fill(
            mint=mint, symbol=symbol, side="sell", ok=True,
            sol_amount=proceeds, token_amount=token_amount,
            price_sol=self.price, fee_sol=0.0005, exit_reason=reason,
        )


class FakeOracle:
    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor

    async def prices(self, mints: list[str]) -> dict[str, float]:
        return {m: self.executor.price for m in mints}

    async def price(self, mint: str, max_age_s: float = 2.0) -> float:
        return self.executor.price


@pytest.fixture
async def setup(tmp_path, config: Config):
    db = Database(tmp_path / "portfolio.db")
    await db.connect()
    executor = FakeExecutor()
    oracle = FakeOracle(executor)
    edge = PersonalEdge(db, min_samples=5)
    await edge.load()
    portfolio = Portfolio(config, db, executor, oracle, edge)  # type: ignore[arg-type]
    yield portfolio, executor, db, config
    await db.close()


class TestOpening:
    async def test_opens_a_position(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        position = await portfolio.open_position(
            good_candidate, 0.05, 75.0, ScoreBreakdown()
        )
        assert position is not None
        assert position.state == PositionState.OPEN
        assert position.tokens_held > 0
        assert portfolio.open_count == 1
        assert executor.buys == [(good_candidate.mint, 0.05)]

    async def test_failed_buy_leaves_no_position(self, setup, good_candidate: Candidate) -> None:
        """A failed buy must not leave a phantom position behind — otherwise
        the mint is blocked forever by something that never existed."""
        portfolio, executor, _db, _config = setup
        executor.fail_buys = True
        assert await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown()) is None
        assert portfolio.open_count == 0
        assert not portfolio.holds(good_candidate.mint)

    async def test_will_not_double_open(self, setup, good_candidate: Candidate) -> None:
        portfolio, _executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        assert await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown()) is None
        assert portfolio.open_count == 1


class TestExitFlow:
    async def test_stop_loss_closes_and_records_loss(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())

        executor.price *= 0.5  # -50%, past the 35% stop
        await portfolio.mark_to_market()
        await portfolio.process_exits()

        assert portfolio.open_count == 0
        assert len(executor.sells) == 1
        assert "stop_loss" in executor.sells[0][2]

        closed = await db.closed_positions()
        assert len(closed) == 1
        assert closed[0]["pnl_sol"] < 0

    async def test_ladder_takes_partial_then_holds(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        position = await portfolio.open_position(
            good_candidate, 0.05, 75.0, ScoreBreakdown()
        )
        assert position is not None
        original = position.tokens_bought

        executor.price *= 1.7  # clears the 1.6x rung
        await portfolio.mark_to_market()
        await portfolio.process_exits()

        assert portfolio.open_count == 1
        assert position.tokens_held < original
        assert position.realised_sol > 0
        assert 0 in position.ladder_filled

    async def test_dev_sell_dumps_everything(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())

        portfolio.note_dev_sell(good_candidate.mint)
        await portfolio.mark_to_market()
        await portfolio.process_exits()

        assert portfolio.open_count == 0
        assert executor.sells[0][2] == "dev_sold"

    async def test_failed_exit_keeps_position_open_for_retry(
        self, setup, good_candidate: Candidate
    ) -> None:
        """If a sell fails the position must stay open and be retried, not be
        silently marked closed while you still hold the tokens."""
        portfolio, executor, _db, _config = setup
        position = await portfolio.open_position(
            good_candidate, 0.05, 75.0, ScoreBreakdown()
        )
        assert position is not None

        executor.price *= 0.5
        executor.sell_failures_remaining = 1

        await portfolio.mark_to_market()
        await portfolio.process_exits()
        assert portfolio.open_count == 1
        assert position.state == PositionState.OPEN

        # Next tick succeeds.
        await portfolio.process_exits()
        assert portfolio.open_count == 0

    async def test_force_close(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        assert await portfolio.force_close(good_candidate.mint, "manual") is True
        assert portfolio.open_count == 0
        assert executor.sells[0][2] == "manual"

    async def test_close_all(self, setup, good_candidate: Candidate) -> None:
        portfolio, _executor, _db, _config = setup
        for i in range(3):
            candidate = good_candidate.model_copy(deep=True)
            candidate.mint = f"MINT{i}" + "x" * 39
            await portfolio.open_position(candidate, 0.02, 75.0, ScoreBreakdown())
        assert portfolio.open_count == 3
        assert await portfolio.close_all("test") == 3
        assert portfolio.open_count == 0


class TestAccounting:
    async def test_daily_counters(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        executor.price *= 3.0
        await portfolio.mark_to_market()
        await portfolio.force_close(good_candidate.mint, "test")

        assert portfolio.closed_today == 1
        assert portfolio.wins_today == 1
        assert portfolio.losses_today == 0
        assert portfolio.daily_pnl_sol > 0

    async def test_loss_counted(self, setup, good_candidate: Candidate) -> None:
        portfolio, executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        executor.price *= 0.3
        await portfolio.mark_to_market()
        await portfolio.force_close(good_candidate.mint, "test")

        assert portfolio.losses_today == 1
        assert portfolio.daily_pnl_sol < 0


class TestLearning:
    async def test_closing_a_trade_teaches_the_edge(
        self, setup, good_candidate: Candidate
    ) -> None:
        portfolio, executor, _db, _config = setup
        assert portfolio.edge is not None
        before = portfolio.edge.trades_learned

        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        executor.price *= 2.0
        await portfolio.mark_to_market()
        await portfolio.force_close(good_candidate.mint, "test")

        assert portfolio.edge.trades_learned == before + 1

    async def test_fast_total_loss_blocks_the_creator(
        self, setup, good_candidate: Candidate
    ) -> None:
        """A token that loses almost everything within minutes is treated as a
        rug, and its creator is blocked from ever being bought again."""
        portfolio, executor, db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())

        executor.price *= 0.02  # -98%
        await portfolio.mark_to_market()
        await portfolio.force_close(good_candidate.mint, "rug")

        assert good_candidate.creator in await db.blocked_creators()

    async def test_ordinary_loss_does_not_block_the_creator(
        self, setup, good_candidate: Candidate
    ) -> None:
        portfolio, executor, db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())

        executor.price *= 0.75  # a normal losing trade
        await portfolio.mark_to_market()
        await portfolio.force_close(good_candidate.mint, "stop_loss")

        assert good_candidate.creator not in await db.blocked_creators()


class TestRecovery:
    async def test_restores_open_positions(self, tmp_path, config: Config, good_candidate) -> None:
        """The most important crash-recovery property: a restarted Brain must
        pick up its open positions, or they sit unmanaged with no stop loss."""
        db_path = tmp_path / "recover.db"

        db1 = Database(db_path)
        await db1.connect()
        executor1 = FakeExecutor()
        portfolio1 = Portfolio(config, db1, executor1, FakeOracle(executor1))  # type: ignore[arg-type]
        await portfolio1.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        assert portfolio1.open_count == 1
        await db1.close()

        # "Restart"
        db2 = Database(db_path)
        await db2.connect()
        executor2 = FakeExecutor()
        portfolio2 = Portfolio(config, db2, executor2, FakeOracle(executor2))  # type: ignore[arg-type]
        assert await portfolio2.restore() == 1
        assert portfolio2.holds(good_candidate.mint)

        restored = portfolio2.positions[good_candidate.mint]
        assert restored.tokens_held > 0
        assert restored.entry_price_sol > 0
        # Exit rules must work immediately on the restored position.
        executor2.price *= 0.4
        await portfolio2.mark_to_market()
        await portfolio2.process_exits()
        assert portfolio2.open_count == 0
        await db2.close()

    async def test_paper_positions_not_restored_into_live(
        self, tmp_path, config: Config, good_candidate
    ) -> None:
        """Restoring a paper position into a live session would make the
        accounting fiction and could trigger a real sell of tokens you do not
        own."""
        db_path = tmp_path / "modes.db"

        db1 = Database(db_path)
        await db1.connect()
        paper_executor = FakeExecutor()
        portfolio1 = Portfolio(config, db1, paper_executor, FakeOracle(paper_executor))  # type: ignore[arg-type]
        await portfolio1.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        await db1.close()

        db2 = Database(db_path)
        await db2.connect()
        live_executor = FakeExecutor()
        live_executor.paper = False
        portfolio2 = Portfolio(config, db2, live_executor, FakeOracle(live_executor))  # type: ignore[arg-type]
        assert await portfolio2.restore() == 0
        await db2.close()


class TestSignalHygiene:
    async def test_signals_pruned_for_closed_positions(
        self, setup, good_candidate: Candidate
    ) -> None:
        portfolio, executor, _db, _config = setup
        await portfolio.open_position(good_candidate, 0.05, 75.0, ScoreBreakdown())
        portfolio.note_dev_sell(good_candidate.mint)
        portfolio.note_migration("some-other-mint")

        await portfolio.force_close(good_candidate.mint, "test")
        portfolio.prune_signals()

        assert portfolio.dev_sold == set()
        assert portfolio.migrated == set()
