# Demo Plan Registry

---

### PLAN_ID: DEMO_P0
anchor: baseline
axis: demo_baseline
intent: Simple 2-layer MLP with ReLU, lr=1e-3, default hyperparams
expect:
  metric: overall_scaffold_balanced_test_mae
  op: lt
  threshold: 0.08
orthogonal_to: []
expand: 1
status: running
