"""Configuration loading and validation.

A typo in YAML must fail loudly at startup, not silently trade with a default
you did not intend.
"""

from __future__ import annotations

import pytest
import yaml

from mctb.config import Config, _deep_merge, load_config


class TestValidation:
    def test_defaults_are_valid(self) -> None:
        assert Config().execution.mode == "paper"

    def test_paper_is_the_default(self) -> None:
        """Fresh config must never be armed for real money."""
        config = Config()
        assert config.is_live is False
        assert config.execution.live_trading_enabled is False

    def test_both_latches_required_for_live(self) -> None:
        config = Config()
        config.execution.mode = "live"
        assert config.is_live is False  # second latch still off

        config.execution.live_trading_enabled = True
        assert config.is_live is True

    def test_enabling_only_the_second_latch_is_not_live(self) -> None:
        config = Config()
        config.execution.live_trading_enabled = True
        assert config.is_live is False

    def test_ladder_over_one_hundred_percent_rejected(self) -> None:
        with pytest.raises(ValueError, match="more than the whole position"):
            Config.model_validate(
                {
                    "exits": {
                        "take_profit_ladder": [
                            {"multiple": 2.0, "fraction": 0.6},
                            {"multiple": 3.0, "fraction": 0.6},
                        ]
                    }
                }
            )

    def test_ladder_multiple_below_one_rejected(self) -> None:
        """A 'take profit' at 0.8x is a stop loss wearing a disguise."""
        with pytest.raises(ValueError, match="sell at a loss"):
            Config.model_validate(
                {"exits": {"take_profit_ladder": [{"multiple": 0.8, "fraction": 0.5}]}}
            )

    def test_ladder_missing_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs both"):
            Config.model_validate({"exits": {"take_profit_ladder": [{"multiple": 2.0}]}})

    @pytest.mark.parametrize("value", [0.0, 1.0, 1.5, -0.2])
    def test_stop_loss_must_be_a_fraction(self, value: float) -> None:
        with pytest.raises(ValueError, match="strictly between 0 and 1"):
            Config.model_validate({"exits": {"stop_loss_pct": value}})

    def test_negative_position_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            Config.model_validate({"risk": {"max_position_sol": -1.0}})

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            Config.model_validate({"execution": {"mode": "yolo"}})


class TestMerging:
    def test_deep_merge_preserves_untouched_keys(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        merged = _deep_merge(base, {"a": {"y": 20}})
        assert merged == {"a": {"x": 1, "y": 20}, "b": 3}

    def test_override_wins(self) -> None:
        assert _deep_merge({"a": 1}, {"a": 2})["a"] == 2

    def test_does_not_mutate_the_base(self) -> None:
        base = {"a": {"x": 1}}
        _deep_merge(base, {"a": {"x": 99}})
        assert base["a"]["x"] == 1


class TestLoading:
    def test_missing_config_gives_a_useful_message(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError, match="cp "):
            load_config(tmp_path / "nope.yaml")

    def test_unknown_preset_lists_available_ones(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "does-not-exist"}))
        with pytest.raises(FileNotFoundError, match="Available presets"):
            load_config(path)

    def test_user_config_beats_preset(self, tmp_path) -> None:
        """Your own edits must always win over the shipped preset."""
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {"strategy_preset": "conservative", "scoring": {"entry_threshold": 51.0}}
            )
        )
        config = load_config(path)
        assert config.scoring.entry_threshold == 51.0
        # ...while unset preset values still come through.
        assert config.gates.require_smart_wallet is True

    def test_preset_applies_when_not_overridden(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "degen"}))
        config = load_config(path)
        assert config.scoring.entry_threshold == 52.0

    def test_role_override(self, tmp_path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "balanced"}))
        assert load_config(path, role="brain").role == "brain"

    def test_env_overrides_yaml(self, tmp_path, monkeypatch) -> None:
        """Secrets and endpoints come from the environment, never from the
        config file that gets committed."""
        monkeypatch.setenv("MCTB_RPC_URL", "https://example.invalid/rpc")
        monkeypatch.setenv("MCTB_PUMPPORTAL_API_KEY", "secret-key")
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": "balanced"}))
        config = load_config(path)
        assert config.rpc.url == "https://example.invalid/rpc"
        assert config.pumpportal_api_key == "secret-key"


class TestPresets:
    @pytest.mark.parametrize("preset", ["conservative", "balanced", "degen"])
    def test_every_preset_is_valid(self, tmp_path, preset: str) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": preset}))
        assert load_config(path) is not None

    @pytest.mark.parametrize("preset", ["conservative", "balanced", "degen"])
    def test_no_preset_arms_live_trading(self, tmp_path, preset: str) -> None:
        """No shipped preset may ever enable real trading."""
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": preset}))
        assert load_config(path).is_live is False

    @pytest.mark.parametrize("preset", ["conservative", "balanced", "degen"])
    def test_authority_checks_never_disabled(self, tmp_path, preset: str) -> None:
        """Mint and freeze authority checks are not a risk trade-off — a token
        that fails them may be literally unsellable. No preset, not even the
        most aggressive, is allowed to turn them off."""
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"strategy_preset": preset}))
        config = load_config(path)
        assert config.gates.require_mint_authority_revoked is True
        assert config.gates.require_freeze_authority_revoked is True

    def test_aggression_reduces_position_size(self, tmp_path) -> None:
        """More aggressive presets must size DOWN, not up — that is what makes
        a low-hit-rate strategy survive its own losing streaks."""
        sizes = {}
        for preset in ("conservative", "balanced", "degen"):
            path = tmp_path / f"{preset}.yaml"
            path.write_text(yaml.safe_dump({"strategy_preset": preset}))
            sizes[preset] = load_config(path).risk.max_position_sol
        assert sizes["degen"] < sizes["balanced"]

    def test_lower_threshold_means_more_aggressive(self, tmp_path) -> None:
        thresholds = {}
        for preset in ("conservative", "balanced", "degen"):
            path = tmp_path / f"{preset}.yaml"
            path.write_text(yaml.safe_dump({"strategy_preset": preset}))
            thresholds[preset] = load_config(path).scoring.entry_threshold
        assert thresholds["conservative"] > thresholds["balanced"] > thresholds["degen"]
