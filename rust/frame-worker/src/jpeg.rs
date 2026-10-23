//! Fixed geometry, baseline-compatible JPEG decode via pinned libjpeg-turbo.
use crate::{MAX_JPEG, detector::Result};
use turbojpeg::{Decompressor, Image, PixelFormat};

fn header(jpeg: &[u8]) -> Result<()> {
    if jpeg.len() > MAX_JPEG || !jpeg.starts_with(&[255, 216]) || !jpeg.ends_with(&[255, 217]) {
        return Err("invalid JPEG envelope".into());
    }
    let mut pos = 2;
    while pos + 1 < jpeg.len() {
        if jpeg[pos] != 255 {
            return Err("invalid JPEG marker".into());
        }
        while pos < jpeg.len() && jpeg[pos] == 255 {
            pos += 1;
        }
        let marker = *jpeg.get(pos).ok_or("truncated JPEG marker")?;
        pos += 1;
        if marker == 218 {
            return Ok(());
        }
        if [0, 1, 216, 217].contains(&marker)
            || (208..=215).contains(&marker)
            || pos + 2 > jpeg.len()
        {
            return Err("unsupported JPEG marker".into());
        }
        let length = u16::from_be_bytes([jpeg[pos], jpeg[pos + 1]]) as usize;
        if length < 2 || pos + length > jpeg.len() {
            return Err("invalid JPEG segment".into());
        }
        if marker == 225 && jpeg[pos + 2..pos + length].starts_with(b"Exif\0\0") {
            return Err("EXIF is outside the PoC contract".into());
        }
        pos += length;
    }
    Err("missing JPEG scan".into())
}

pub fn decode(jpeg: &[u8]) -> Result<(Vec<u8>, usize)> {
    header(jpeg)?;
    let mut decoder = Decompressor::new()?;
    decoder.set_fast_upsample(false)?;
    decoder.set_scan_limit(64)?;
    let info = decoder.read_header(jpeg)?;
    if info.width != 320 || ![240, 320].contains(&info.height) {
        return Err("unsupported JPEG dimensions".into());
    }
    let mut pixels = vec![0; 320 * info.height * 3];
    decoder.decompress(
        jpeg,
        Image {
            pixels: &mut pixels[..],
            width: 320,
            height: info.height,
            pitch: 320 * 3,
            format: PixelFormat::RGB,
        },
    )?;
    Ok((pixels, info.height))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn black(width: usize, height: usize) -> Vec<u8> {
        turbojpeg::compress(
            Image {
                pixels: vec![0; width * height * 3].as_slice(),
                width,
                height,
                pitch: width * 3,
                format: PixelFormat::RGB,
            },
            95,
            turbojpeg::Subsamp::None,
        )
        .unwrap()
        .to_vec()
    }

    #[test]
    fn black_rgb_dimensions_and_bounded_geometry() {
        for height in [240, 320] {
            let (rgb, actual) = decode(&black(320, height)).unwrap();
            assert_eq!(actual, height);
            assert_eq!(rgb, vec![0; 320 * height * 3]);
        }
        assert!(decode(&black(321, 240)).is_err());
        assert!(decode(&black(320, 241)).is_err());
    }

    #[test]
    fn malformed_truncated_oversized_and_exif_inputs_rejected() {
        for data in [
            vec![],
            vec![255, 216, 255, 217],
            vec![0; MAX_JPEG + 1],
            vec![255, 216, 255, 225, 0, 1, 255, 217],
        ] {
            assert!(decode(&data).is_err());
        }
        let valid = black(320, 240);
        assert!(decode(&valid[..valid.len() - 1]).is_err());
        let mut exif = valid[..2].to_vec();
        exif.extend(b"\xff\xe1\x00\x08Exif\x00\x00");
        exif.extend(&valid[2..]);
        assert!(decode(&exif).is_err());
    }
}
