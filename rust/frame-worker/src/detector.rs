//! Fixed Pi profile. No runtime/model downloads and no live result publication.
use ort::{
    session::{Session, builder::GraphOptimizationLevel},
    value::TensorRef,
};
use sha2::{Digest, Sha256};
use std::{fs::File, io::Read, path::Path};

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const MODEL_SHA: &str = "ff1013da8734fe2b8ede5f34a2124855d447fbefd248d148e9f29e7f8e28c9c4";
// Exact ARM64 wheel library already used by the Pi Python baseline (ORT1.23.2).
pub const RUNTIME_SHA: &str = "ab8c4363e06ac80b3d1279ea55ebea44e906c5b131ba783ff684a067540c0e94";
pub const INPUT_LEN: usize = 3 * 320 * 320;
pub const OUTPUT_LEN: usize = 84 * 2100;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TaskPartition {
    #[default]
    Static,
    Dynamic4,
}
impl TaskPartition {
    /// Explicit experiment setting; library defaults never read the environment.
    pub fn parse(value: std::result::Result<String, std::env::VarError>) -> Result<Self> {
        match value {
            Err(std::env::VarError::NotPresent) => Ok(Self::Static),
            Ok(value) if value == "0" => Ok(Self::Static),
            Ok(value) if value == "1" && cfg!(target_os = "linux") => Ok(Self::Dynamic4),
            _ => Err("invalid dynamic task partition setting".into()),
        }
    }
    pub fn base(self) -> Option<u32> {
        match self {
            Self::Static => None,
            Self::Dynamic4 => Some(4),
        }
    }
}

fn geometry(height: usize) -> Result<usize> {
    if ![240, 320].contains(&height) {
        return Err("unsupported image geometry".into());
    }
    Ok((320 - height) / 2)
}

/// Current QVGA and square fixtures need no resampling. Input must be RGB8.
pub fn prepare_rgb(rgb: &[u8], height: usize) -> Result<Vec<f32>> {
    let top = geometry(height)?;
    if rgb.len() != 320 * height * 3 {
        return Err("invalid RGB length".into());
    }
    let mut tensor = vec![114.0 / 255.0; INPUT_LEN];
    for y in 0..height {
        for x in 0..320 {
            for c in 0..3 {
                tensor[c * 320 * 320 + (y + top) * 320 + x] =
                    rgb[(y * 320 + x) * 3 + c] as f32 / 255.0;
            }
        }
    }
    Ok(tensor)
}

#[derive(Clone, Debug, PartialEq)]
pub struct Detection {
    pub score: f32,
    pub bbox: [f32; 4],
}

struct Candidate {
    class: usize,
    score: f32,
    bbox: [f32; 4],
    area: f32,
}

fn iou(a: &Candidate, b: &Candidate) -> f32 {
    let w = (a.bbox[2].min(b.bbox[2]) - a.bbox[0].max(b.bbox[0])).max(0.0);
    let h = (a.bbox[3].min(b.bbox[3]) - a.bbox[1].max(b.bbox[1])).max(0.0);
    let intersection = w * h;
    intersection / (a.area + b.area - intersection).max(1e-9)
}

pub fn postprocess(raw: &[f32], height: usize) -> Result<Vec<Detection>> {
    let top = geometry(height)? as f32;
    if raw.len() != OUTPUT_LEN || raw.iter().any(|v| !v.is_finite()) {
        return Err("invalid YOLO output".into());
    }
    let mut candidates = Vec::new();
    for anchor in 0..2100 {
        let value = |channel: usize| raw[channel * 2100 + anchor];
        let mut class = 0;
        for c in 1..80 {
            if value(c + 4) > value(class + 4) {
                class = c;
            }
        }
        let score = value(class + 4);
        if score < 0.5 {
            continue;
        }
        if score > 1.0 || value(2) <= 0.0 || value(3) <= 0.0 {
            return Err("invalid candidate".into());
        }
        let bbox = [
            value(0) - value(2) / 2.0,
            value(1) - value(3) / 2.0,
            value(0) + value(2) / 2.0,
            value(1) + value(3) / 2.0,
        ];
        let area = (bbox[2] - bbox[0]).max(0.0) * (bbox[3] - bbox[1]).max(0.0);
        if !area.is_finite() || bbox.iter().any(|v| !v.is_finite()) {
            return Err("overflowing candidate".into());
        }
        candidates.push(Candidate {
            class,
            score,
            bbox,
            area,
        });
    }
    let mut order: Vec<usize> = (0..candidates.len()).collect();
    order.sort_unstable_by(|&a, &b| candidates[b].score.total_cmp(&candidates[a].score));
    // NumPy's default argsort does not define equal-score ordering. Until that
    // policy is explicitly reconciled, refuse an ambiguous PoC result.
    if order
        .windows(2)
        .any(|w| candidates[w[0]].score == candidates[w[1]].score)
    {
        return Err("ambiguous equal-score ordering".into());
    }
    let mut result = Vec::new();
    let mut kept = 0;
    while !order.is_empty() && kept < 300 {
        let index = order.remove(0);
        let c = &candidates[index];
        kept += 1; // All classes consume the frozen max_det budget, not just cats.
        if c.class == 15 {
            let b = c.bbox;
            let bbox = [
                (b[0] / 320.0).clamp(0.0, 1.0),
                ((b[1] - top) / height as f32).clamp(0.0, 1.0),
                (b[2] / 320.0).clamp(0.0, 1.0),
                ((b[3] - top) / height as f32).clamp(0.0, 1.0),
            ];
            if bbox[0] < bbox[2] && bbox[1] < bbox[3] {
                result.push(Detection {
                    score: c.score,
                    bbox,
                });
            }
        }
        order.retain(|&other| {
            candidates[other].class != c.class || iou(c, &candidates[other]) <= 0.7
        });
    }
    Ok(result)
}

pub fn verify_runtime(path: &Path) -> Result<()> {
    let mut file = File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut total = 0;
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        total += read;
        if total > 64 * 1024 * 1024 {
            return Err("runtime too large".into());
        }
        hash.update(&buffer[..read]);
    }
    if format!("{:x}", hash.finalize()) != RUNTIME_SHA {
        return Err("runtime hash mismatch".into());
    }
    Ok(())
}

pub struct Detector {
    session: Session,
}

impl Detector {
    pub fn new(model: &Path, runtime: &Path) -> Result<Self> {
        Self::new_with_task_partition(model, runtime, TaskPartition::Static)
    }

    pub fn new_with_task_partition(
        model: &Path,
        runtime: &Path,
        partition: TaskPartition,
    ) -> Result<Self> {
        let runtime = runtime.canonicalize()?;
        verify_runtime(&runtime)?;
        let mut bytes = Vec::new();
        File::open(model)?
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        if bytes.len() > 16 * 1024 * 1024 || format!("{:x}", Sha256::digest(&bytes)) != MODEL_SHA {
            return Err("model hash mismatch".into());
        }
        ort::init_from(&runtime)?.commit();
        let builder = Session::builder()?
            .with_intra_threads(3)?
            .with_inter_threads(1)?
            .with_parallel_execution(false)?
            .with_intra_op_spinning(false)?
            .with_inter_op_spinning(false)?
            .with_optimization_level(GraphOptimizationLevel::All)?;
        let builder = if partition == TaskPartition::Dynamic4 {
            builder.with_config_entry("session.dynamic_block_base", "4")?
        } else {
            builder
        };
        let session = builder.commit_from_memory(&bytes)?;
        if session.inputs().len() != 1 || session.outputs().len() != 1 {
            return Err("invalid model IO".into());
        }
        Ok(Self { session })
    }

    pub fn infer(&mut self, tensor: &[f32]) -> Result<Vec<f32>> {
        if tensor.len() != INPUT_LEN
            || tensor
                .iter()
                .any(|v| !v.is_finite() || !(0.0..=1.0).contains(v))
        {
            return Err("invalid input tensor".into());
        }
        let input = TensorRef::from_array_view(([1, 3, 320, 320], tensor))?;
        let output = self.session.run(ort::inputs![input])?;
        let (shape, raw) = output[0].try_extract_tensor::<f32>()?;
        if shape.as_ref() != [1, 84, 2100] || raw.iter().any(|v| !v.is_finite()) {
            return Err("invalid output tensor".into());
        }
        Ok(raw.to_vec())
    }
}

#[cfg(test)]
mod tests;
