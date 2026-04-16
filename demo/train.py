#!/usr/bin/env python3
"""
Demo training script — mimics KERMT training log format so poll_tick can parse it.

Usage:
    python demo/train.py [--epochs N] [--lr LR] [--result_dir demo/results/run1]
"""

import argparse
import csv
import time
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from model import RfPredictor

DEMO_DIR = Path(__file__).parent
FIELDS = ["H", "EA", "DCM", "MeOH", "Et2O", "MW", "LogP"]


def load_csv(path: Path) -> tuple[torch.Tensor, torch.Tensor]:
    xs, ys = [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            xs.append([float(row[k]) for k in FIELDS])
            ys.append(float(row["Rf"]))
    return torch.tensor(xs, dtype=torch.float32), torch.tensor(ys, dtype=torch.float32)


def evaluate(model: nn.Module, loader: DataLoader) -> tuple[float, float]:
    model.eval()
    total_loss, total_ae, n = 0.0, 0.0, 0
    criterion = nn.MSELoss(reduction="sum")
    with torch.no_grad():
        for xb, yb in loader:
            pred = model(xb)
            total_loss += criterion(pred, yb).item()
            total_ae += (pred - yb).abs().sum().item()
            n += len(yb)
    return total_loss / n, total_ae / n


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--result_dir", type=str, default="demo/results/baseline")
    parser.add_argument("--early_stop", type=int, default=10)
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "nohup_train.log"

    x_train, y_train = load_csv(DEMO_DIR / "train.csv")
    x_val, y_val = load_csv(DEMO_DIR / "valid.csv")
    x_test, y_test = load_csv(DEMO_DIR / "test.csv")

    train_loader = DataLoader(TensorDataset(x_train, y_train),
                              batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(TensorDataset(x_val, y_val), batch_size=256)
    test_loader = DataLoader(TensorDataset(x_test, y_test), batch_size=256)

    model = RfPredictor(input_dim=len(FIELDS))
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.MSELoss()

    with open(log_path, "w") as log:
        header = (
            f"========================================================\n"
            f"  Demo TLC Training\n"
            f"========================================================\n"
            f"  Epochs:     {args.epochs}\n"
            f"  Batch size: {args.batch_size}\n"
            f"  LR:         {args.lr}\n"
            f"  Early stop: {args.early_stop} epochs without val improvement\n"
            f"========================================================\n"
        )
        log.write(header)
        print(header, end="")

        best_val_mae = float("inf")
        best_epoch = 0
        patience_counter = 0

        for epoch in range(args.epochs):
            t0 = time.time()
            model.train()
            epoch_loss = 0.0
            for xb, yb in train_loader:
                pred = model(xb)
                loss = criterion(pred, yb)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item() * len(yb)

            train_loss = epoch_loss / len(x_train)
            val_loss, val_mae = evaluate(model, val_loader)
            t_time = time.time() - t0

            line = (
                f"Epoch: {epoch:04d} loss_train: {train_loss:.6f} "
                f"loss_val: {val_loss:.6f} mae_val: {val_mae:.4f} "
                f"cur_lr: {args.lr:.6f} t_time: {t_time:.1f}s v_time: 0.0s\n"
            )
            log.write(line)
            log.flush()
            print(line, end="")

            if val_mae < best_val_mae:
                best_val_mae = val_mae
                best_epoch = epoch
                patience_counter = 0
                torch.save(model.state_dict(), result_dir / "best_model.pt")
            else:
                patience_counter += 1

            if patience_counter >= args.early_stop:
                msg = f"\nEarly stopping at epoch {epoch} (no improvement for {args.early_stop} epochs)\n"
                log.write(msg)
                print(msg, end="")
                break

        model.load_state_dict(torch.load(result_dir / "best_model.pt",
                                         weights_only=True))
        _, test_mae = evaluate(model, test_loader)

        footer = (
            f"\nBest val MAE: {best_val_mae:.4f} at epoch {best_epoch}\n"
            f"Model 0 test mae = {test_mae:.6f}\n"
            f"overall_scaffold_balanced_test_mae={test_mae:.6f}\n"
            f"\nTraining complete. Model saved to: {result_dir}\n"
        )
        log.write(footer)
        print(footer, end="")


if __name__ == "__main__":
    main()
