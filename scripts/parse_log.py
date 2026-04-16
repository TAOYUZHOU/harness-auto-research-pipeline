#!/usr/bin/env python3
"""Extract metrics and hyperparameters from a KERMT TLC training run."""

import os
import re
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunResult:
    anchor: str
    test_mae: Optional[float] = None
    best_val_mae: Optional[float] = None
    best_epoch: Optional[int] = None
    total_epochs: int = 0
    hyperparams: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.test_mae is not None and self.error is None

    @property
    def is_complete(self) -> bool:
        """Training finished (has test_mae line)."""
        return self.test_mae is not None

    def hp_summary(self) -> str:
        keys = [
            "max_lr", "init_lr", "final_lr", "weight_decay", "dropout",
            "batch_size", "epochs", "early_stop_epoch", "ffn_hidden_size",
            "ffn_num_layers", "solvent_emb_dim", "regression_loss",
        ]
        parts = []
        for k in keys:
            if k in self.hyperparams:
                parts.append(f"{k}={self.hyperparams[k]}")
        return ",".join(parts) if parts else "none"


_EPOCH_RE = re.compile(
    r"Epoch:\s*(\d+)\s+loss_train:\s*([\d.]+)\s+loss_val:\s*([\d.]+)"
    r"\s+mae_val:\s*([\d.]+)"
)
_TEST_MAE_RE = re.compile(r"overall_scaffold_balanced_test_mae=([\d.]+)")
_MODEL_TEST_RE = re.compile(r"Model\s+\d+\s+test\s+mae\s*=\s*([\d.]+)")
_BEST_VAL_RE = re.compile(r"Best val MAE:\s*([\d.]+)\s+at epoch\s+(\d+)")
_TRAINING_COMPLETE_RE = re.compile(r"(?:Training complete|Model saved to:)")


def parse_training_log(log_path: str) -> RunResult:
    anchor = Path(log_path).parent.name
    result = RunResult(anchor=anchor)

    try:
        with open(log_path, "r", errors="replace") as f:
            text = f.read()
    except OSError as e:
        result.error = str(e)
        return result

    best_val = float("inf")
    best_ep = -1
    last_ep = -1

    for m in _EPOCH_RE.finditer(text):
        ep = int(m.group(1))
        mae_v = float(m.group(4))
        last_ep = max(last_ep, ep)
        if mae_v < best_val:
            best_val = mae_v
            best_ep = ep

    if best_ep >= 0:
        result.best_val_mae = round(best_val, 6)
        result.best_epoch = best_ep
        result.total_epochs = last_ep + 1

    bv = _BEST_VAL_RE.search(text)
    if bv:
        result.best_val_mae = float(bv.group(1))
        result.best_epoch = int(bv.group(2))

    m = _TEST_MAE_RE.search(text)
    if m:
        result.test_mae = float(m.group(1))
    else:
        m = _MODEL_TEST_RE.search(text)
        if m:
            result.test_mae = float(m.group(1))

    if result.test_mae is None and _TRAINING_COMPLETE_RE.search(text):
        result.test_mae = result.best_val_mae

    config_path = Path(log_path).parent / "effective_config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            result.hyperparams = {k: v for k, v in cfg.items()
                                  if not isinstance(v, (list, dict))}
        except Exception:
            pass

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: parse_log.py <path/to/nohup_train.log>")
        sys.exit(1)
    r = parse_training_log(sys.argv[1])
    print(f"anchor:       {r.anchor}")
    print(f"test_mae:     {r.test_mae}")
    print(f"best_val_mae: {r.best_val_mae} (epoch {r.best_epoch})")
    print(f"total_epochs: {r.total_epochs}")
    print(f"complete:     {r.is_complete}")
    print(f"HP:           {r.hp_summary()}")
    if r.error:
        print(f"ERROR:        {r.error}")
