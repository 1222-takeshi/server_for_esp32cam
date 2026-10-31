//! Finite offline experiment worker. It neither listens on a port nor drives motors.
use esp32cam_frame_worker::{
    detector::{Detector, Result, TaskPartition, postprocess, prepare_rgb},
    jpeg, replay,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{io, path::Path, time::Instant};

#[cfg(target_endian = "big")]
compile_error!("The frozen tensor byte format requires little endian");

fn execute() -> Result<()> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 3 {
        return Err("usage: frame-worker MODEL_ONNX ORT_LIBRARY < frames.ecf2".into());
    }
    let partition = TaskPartition::parse(std::env::var("ESP32CAM_RUST_BENCH_DYNAMIC_BLOCKS"))?;
    let mut detector =
        Detector::new_with_task_partition(Path::new(&args[1]), Path::new(&args[2]), partition)?;
    let wall = Instant::now();
    replay::run(&mut io::stdin().lock(), &mut io::stdout().lock(), |frame| {
        let start = Instant::now();
        let (rgb, height) = jpeg::decode(&frame.jpeg)?;
        let decoded = start.elapsed();
        let tensor = prepare_rgb(&rgb, height)?;
        let prepared = start.elapsed();
        let raw = detector.infer(&tensor)?;
        let inferred = start.elapsed();
        let detections = postprocess(&raw, height)?;
        let total = start.elapsed();
        let detections: Vec<_> = detections
            .iter()
            .map(|d| {
                json!({"label":"cat","score":d.score,
            "x1":d.bbox[0],"y1":d.bbox[1],"x2":d.bbox[2],"y2":d.bbox[3],"track_id":null})
            })
            .collect();
        let input_sha = format!(
            "{:x}",
            Sha256::digest(bytemuck::cast_slice::<f32, u8>(&tensor))
        );
        let raw_sha = format!(
            "{:x}",
            Sha256::digest(bytemuck::cast_slice::<f32, u8>(&raw))
        );
        Ok(
            json!({"dynamic_block_base":partition.base(),"runtime_build":ort::info(),"input_sha256":input_sha,"raw_sha256":raw_sha,"detections":detections,
            "decode_us":decoded.as_micros(),"prepare_us":(prepared-decoded).as_micros(),
            "infer_us":(inferred-prepared).as_micros(),"post_us":(total-inferred).as_micros(),
            "total_us":total.as_micros(),"wall_us":wall.elapsed().as_micros(),"usage":replay::usage()}),
        )
    })?;
    Ok(())
}

fn main() {
    if execute().is_err() {
        eprintln!("Replay failed: check bounded input, fixed model/runtime and output destination");
        std::process::exit(2);
    }
}
