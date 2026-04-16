"""Demo regression model with AGENT-EDITABLE blocks."""

import torch
import torch.nn as nn


class RfPredictor(nn.Module):
    """Predict TLC Rf from solvent ratios + molecular descriptors."""

    def __init__(self, input_dim: int = 7):
        super().__init__()

        # AGENT-EDITABLE-BEGIN
        hidden_dim = 64
        n_layers = 2
        activation = nn.ReLU
        dropout = 0.1
        # AGENT-EDITABLE-END

        layers = []
        prev = input_dim
        for _ in range(n_layers):
            layers.extend([nn.Linear(prev, hidden_dim), activation(), nn.Dropout(dropout)])
            prev = hidden_dim
        layers.append(nn.Linear(prev, 1))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)
