# Reviewed Cat annotation overlay

Use `scripts/apply_cat_annotation_overlay.py` to derive a new private training dataset from the immutable experiment-005 dataset after **every** image has an annotation review. The original images, labels, split manifest, and `dataset.yaml` remain unchanged. Keep the review ledger and derived files outside Git. This does not train or deploy a model.

The private ledger is JSON version 1. Its two source hashes bind it to one exact dataset. `review_id` identifies the audit. `auditors` is optional provenance, while **each entry's** `reviewers` records who actually reviewed that image; a global list does not assert that everyone checked all images. Include exactly one entry per source `image_id` with the original image/label SHA-256, `split`, `kind`, and `cohort`:

```json
{
  "version": 1,
  "source_split_manifest_sha256": "<64 lowercase hex>",
  "source_dataset_yaml_sha256": "<64 lowercase hex>",
  "review_id": "cat-audit-001",
  "auditors": ["first-reviewer", "second-reviewer"],
  "entries": [
    {"image_id": "<16 lowercase hex>", "image_sha256": "<64 lowercase hex>",
     "label_sha256": "<64 lowercase hex>", "split": "train", "kind": "positive",
     "cohort": "new", "status": "keep", "reviewers": ["first-reviewer"]}
  ]
}
```

`keep` requires at least one reviewer and copies both files byte-for-byte. `quarantine` requires a nonempty `reason` and at least two distinct reviewers, then omits the image and label only from the new dataset. `replace_boxes` requires a reason, two distinct reviewers, and **all** visible live Cat boxes as normalized `[xmin,ymin,xmax,ymax]` coordinates; it writes YOLO class 15 labels. An empty `boxes: []` explicitly changes a mistaken positive to negative. Nonempty boxes can change a mistaken negative to positive. The delta records both source and derived kinds. Reviewers must distinguish a live household cat from printed depictions, wild cats, or ambiguous images; quarantine uncertainty rather than inventing boxes. A second reviewer must agree on every changed entry before applying the ledger.

```sh
python -m scripts.apply_cat_annotation_overlay \
  --source-dataset /private/experiment-005/dataset \
  --ledger /private/reviews/cat-audit-001.json \
  --output /private/experiment-006/dataset
```

The output must be a new path outside the source and Git. Files and directories use modes `0600` and `0700`. Input image and label hashes, ledger completeness, class mapping, and source groups are checked before output; original files and ledger are checked again before final metadata. A failed run may leave incomplete private files; retry at a fresh path. `review-delta.json` records all review decisions, reviewers, reasons, box replacements, and before/after label hashes. The new split manifest records source/ledger/delta hashes, all review counts, derived counts by kind/cohort/split, surviving groups, and image/label hashes. Existing positive group IDs are retained with quarantined or reclassified IDs filtered out. A negative changed to positive receives an explicit singleton group in its original split; no image changes split or cohort. These public images remain development data, not evidence of household-camera precision or recall.
