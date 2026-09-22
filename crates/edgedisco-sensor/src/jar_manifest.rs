//! Bounded ZIP/JAR lookup for one exact plugin manifest path.
#[cfg(target_os = "macos")]
use std::io::Read;

#[cfg(target_os = "macos")]
fn u16_at(data: &[u8], offset: usize) -> Option<u16> {
    Some(u16::from_le_bytes(
        data.get(offset..offset.checked_add(2)?)?.try_into().ok()?,
    ))
}

#[cfg(target_os = "macos")]
fn u32_at(data: &[u8], offset: usize) -> Option<u32> {
    Some(u32::from_le_bytes(
        data.get(offset..offset.checked_add(4)?)?.try_into().ok()?,
    ))
}

/// Returns at most 128 KiB from an unencrypted stored/deflated entry.
#[cfg(target_os = "macos")]
pub(crate) fn plugin_xml(jar: &[u8]) -> Option<Vec<u8>> {
    const NAME: &[u8] = b"META-INF/plugin.xml";
    const MAX_XML: usize = 131_072;
    const MAX_JAR: usize = 64 * 1024 * 1024;
    if jar.len() > MAX_JAR || jar.len() < 22 {
        return None;
    }
    let eocd = (jar.len().saturating_sub(65_557)..=jar.len() - 22)
        .rev()
        .find(|&at| {
            u32_at(jar, at) == Some(0x0605_4b50)
                && at.checked_add(22 + u16_at(jar, at + 20).unwrap_or(0) as usize)
                    == Some(jar.len())
        })?;
    if u16_at(jar, eocd + 4)? != 0 || u16_at(jar, eocd + 6)? != 0 {
        return None;
    }
    let entries = u16_at(jar, eocd + 10)? as usize;
    if entries > 4096 || entries != u16_at(jar, eocd + 8)? as usize {
        return None;
    }
    let central_size = u32_at(jar, eocd + 12)? as usize;
    let mut at = u32_at(jar, eocd + 16)? as usize;
    let central_end = at.checked_add(central_size)?;
    if central_end > eocd {
        return None;
    }
    for _ in 0..entries {
        if u32_at(jar, at)? != 0x0201_4b50 {
            return None;
        }
        let flags = u16_at(jar, at + 8)?;
        let method = u16_at(jar, at + 10)?;
        let crc = u32_at(jar, at + 16)?;
        let compressed = u32_at(jar, at + 20)? as usize;
        let expanded = u32_at(jar, at + 24)? as usize;
        let name_len = u16_at(jar, at + 28)? as usize;
        let extra_len = u16_at(jar, at + 30)? as usize;
        let comment_len = u16_at(jar, at + 32)? as usize;
        let local = u32_at(jar, at + 42)? as usize;
        let end = at
            .checked_add(46)?
            .checked_add(name_len)?
            .checked_add(extra_len)?
            .checked_add(comment_len)?;
        if end > central_end {
            return None;
        }
        if jar.get(at + 46..at + 46 + name_len)? == NAME {
            if flags & 1 != 0
                || !matches!(method, 0 | 8)
                || compressed > MAX_XML
                || expanded > MAX_XML
            {
                return None;
            }
            if u32_at(jar, local)? != 0x0403_4b50 || u16_at(jar, local + 8)? != method {
                return None;
            }
            let local_name = u16_at(jar, local + 26)? as usize;
            let local_extra = u16_at(jar, local + 28)? as usize;
            if jar.get(local + 30..local + 30 + local_name)? != NAME {
                return None;
            }
            let start = local
                .checked_add(30)?
                .checked_add(local_name)?
                .checked_add(local_extra)?;
            let input = jar.get(start..start.checked_add(compressed)?)?;
            let output = if method == 0 {
                input.to_vec()
            } else {
                let mut output = Vec::new();
                flate2::read::DeflateDecoder::new(input)
                    .take((MAX_XML + 1) as u64)
                    .read_to_end(&mut output)
                    .ok()?;
                output
            };
            return (output.len() == expanded && crc32fast::hash(&output) == crc).then_some(output);
        }
        at = end;
    }
    None
}
