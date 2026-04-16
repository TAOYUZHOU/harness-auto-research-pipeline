# Auto-Research Plan Registry

# Global stop condition
# ---
# global_stop:
#   metric: overall_scaffold_balanced_test_mae
#   op: lt
#   threshold: 0.04

---

### PLAN_ID: P0
anchor: with_features_v1_dirty
axis: baseline
intent: Grover finetune with 21-dim solvent+descriptor features, default hyperparams
expect:
  metric: overall_scaffold_balanced_test_mae
  op: lt
  threshold: 0.08
orthogonal_to: []
expand: 1
status: completed

---

### PLAN_ID: P1
anchor: with_features_grover_scratch_datav2_ep200_es50_bs128
axis: data_version_and_pretrain
intent: Grover from pretrained base (not finetuned ckpt) on data_v2_cleaned, ep200 es50
expect:
  metric: overall_scaffold_balanced_test_mae
  op: lt
  threshold: 0.075
orthogonal_to: [P0]
expand: 1
status: running

---

### PLAN_ID: P2
anchor: c_v3_vector_datav2_ep200_es100_bs128
axis: architecture_vector_regression
intent: Single-head T-dim Rf vector output, no sequential decoding, pure parallel regression
expect:
  metric: overall_scaffold_balanced_test_mae
  op: lt
  threshold: 0.08
orthogonal_to: [P0, P1]
expand: 1
status: completed
