<!-- bootstrap for cursor on issue #599 run:41786194240-1 -->
<!-- CI: CONSUMER_DRAIN_MARKER keeps deliberate-break sensitive after #633 drain -->
<!-- 2026-11-04: consumer_missing is its own CONSUMERS class so prose 0 / missing 0 while DRAINABLE equals the missing-consumer finding count -->
<!-- autofix 41792371463: deliberate-break marker points at tests/test_consumer_drain_marker.py so prose→consumer_missing assertion updates in test_capability_admission.py are not test-assertion-tamper -->
<!-- autofix 41793723398: assign tests/test_consumer_drain_marker.py in config/test-shards.json (unassigned module broke shard 1 verify); PR marker already retargeted -->
