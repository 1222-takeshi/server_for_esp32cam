use super::*;

fn prediction(raw: &mut [f32], index: usize, class: usize, score: f32, x: f32) {
    for (channel, value) in [x, 160.0, 80.0, 80.0].into_iter().enumerate() {
        raw[channel * 2100 + index] = value;
    }
    raw[(class + 4) * 2100 + index] = score;
}

#[test]
fn rgb_channel_order_normalization_padding_and_dimensions() {
    let mut rgb = vec![0; 320 * 240 * 3];
    rgb[..3].copy_from_slice(&[255, 128, 0]);
    let tensor = prepare_rgb(&rgb, 240).unwrap();
    assert_eq!(tensor.len(), INPUT_LEN);
    assert_eq!(tensor[0], 114.0 / 255.0);
    assert_eq!(tensor[40 * 320], 1.0);
    assert_eq!(tensor[320 * 320 + 40 * 320], 128.0 / 255.0);
    assert_eq!(tensor[2 * 320 * 320 + 40 * 320], 0.0);
    assert!(prepare_rgb(&rgb, 320).is_err());
    assert!(prepare_rgb(&rgb, usize::MAX).is_err());
    assert!(
        prepare_rgb(&vec![255; INPUT_LEN], 320)
            .unwrap()
            .iter()
            .all(|v| *v == 1.0)
    );
}

#[test]
fn class_aware_nms_cat_filter_and_deletterboxing() {
    let mut raw = vec![0.0; OUTPUT_LEN];
    prediction(&mut raw, 0, 15, 0.9, 160.0);
    prediction(&mut raw, 1, 15, 0.8, 161.0); // Same-class overlap suppressed.
    prediction(&mut raw, 2, 0, 0.95, 160.0); // Other class must not suppress cat.
    prediction(&mut raw, 3, 15, 0.7, 280.0);
    let detections = postprocess(&raw, 240).unwrap();
    assert_eq!(detections.len(), 2);
    assert_eq!(
        detections[0],
        Detection {
            score: 0.9,
            bbox: [0.375, 1.0 / 3.0, 0.625, 2.0 / 3.0]
        }
    );
    assert_eq!(detections[1].score, 0.7);
}

#[test]
fn threshold_boundary_clipping_and_max_det_counts_all_classes() {
    let mut raw = vec![0.0; OUTPUT_LEN];
    prediction(&mut raw, 0, 15, 0.5, -20.0);
    assert_eq!(postprocess(&raw, 320).unwrap()[0].bbox[0], 0.0);
    raw[19 * 2100] = 0.499;
    assert!(postprocess(&raw, 320).unwrap().is_empty());
    for i in 0..300 {
        prediction(&mut raw, i, 0, 0.99 - i as f32 / 1000.0, i as f32 * 100.0);
    }
    prediction(&mut raw, 300, 15, 0.5, 160.0);
    assert!(postprocess(&raw, 320).unwrap().is_empty());
}

#[test]
fn invalid_output_and_ambiguous_ties_fail_closed() {
    let mut raw = vec![0.0; OUTPUT_LEN];
    assert!(postprocess(&raw[..10], 320).is_err());
    assert!(postprocess(&raw, 0).is_err());
    for value in [f32::NAN, f32::INFINITY] {
        raw[0] = value;
        assert!(postprocess(&raw, 320).is_err());
    }
    raw[0] = 0.0;
    prediction(&mut raw, 0, 15, 1.1, 160.0);
    assert!(postprocess(&raw, 320).is_err());
    prediction(&mut raw, 0, 15, 0.9, 160.0);
    raw[2 * 2100] = 0.0;
    assert!(postprocess(&raw, 320).is_err());
    prediction(&mut raw, 0, 15, 0.9, 160.0);
    prediction(&mut raw, 1, 15, 0.9, 280.0);
    assert!(postprocess(&raw, 320).is_err());
}

#[test]
fn wrong_runtime_rejected_before_native_code_load() {
    assert!(verify_runtime(Path::new("Cargo.toml")).is_err());
    assert!(Detector::new(Path::new("missing-model"), Path::new("Cargo.toml")).is_err());
}
