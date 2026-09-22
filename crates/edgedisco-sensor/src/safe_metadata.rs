//! Bounded, no-follow reads for untrusted local metadata on macOS.
use std::ffi::{CString, OsStr};
use std::fs::{File, OpenOptions};
use std::io::{Read, Result};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

pub(crate) fn open_dir(path: &Path) -> Result<File> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(path)
}

pub(crate) fn open_child(parent: &File, name: &OsStr, flags: i32) -> Option<File> {
    let name = CString::new(name.as_bytes()).ok()?;
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | flags,
        )
    };
    if fd < 0 {
        None
    } else {
        Some(unsafe { File::from_raw_fd(fd) })
    }
}

pub(crate) fn read_regular(parent: &File, name: &OsStr, max_bytes: u64) -> Option<Vec<u8>> {
    let mut file = open_child(parent, name, libc::O_NONBLOCK)?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > max_bytes {
        return None;
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(max_bytes + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    (bytes.len() as u64 <= max_bytes).then_some(bytes)
}
