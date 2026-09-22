use crate::process::fallback::FallbackProcessScanner;
use crate::process::{ProcessObservation, ProcessScanError, ProcessScanner};

#[cfg(target_os = "macos")]
use libc::{
    c_int, c_void, pid_t, proc_bsdinfo, proc_listpids, proc_pidinfo, proc_pidpath, sysctl,
    CTL_KERN, KERN_PROCARGS2, PROC_PIDPATHINFO_MAXSIZE, PROC_PIDTBSDINFO,
};

#[cfg(target_os = "macos")]
const PROC_ALL_PIDS: u32 = 1;

/// Darwin / macOS process scanner utilizing native `libproc` C-ABI and `sysctl` kernel APIs.
#[derive(Debug, Default, Clone, Copy)]
pub struct DarwinProcessScanner;

impl DarwinProcessScanner {
    pub fn new() -> Self {
        Self
    }

    #[cfg(target_os = "macos")]
    fn scan_native(&self) -> Result<Vec<ProcessObservation>, ProcessScanError> {
        let pids = list_pids()?;
        let mut observations = Vec::with_capacity(pids.len());

        for pid in pids {
            let executable = pid_path(pid).unwrap_or_default();
            let (ppid, bsd_name) = pid_info(pid).unwrap_or((0, String::new()));

            let effective_exe = if !executable.is_empty() {
                executable
            } else if !bsd_name.is_empty() {
                bsd_name
            } else {
                continue;
            };

            let args = pid_cmdline(pid).unwrap_or_else(|| vec![effective_exe.clone()]);

            let ppid_opt = if ppid > 0 { Some(ppid) } else { None };
            observations.push(ProcessObservation::new(
                Some(pid),
                ppid_opt,
                effective_exe,
                args,
            ));
        }

        if observations.is_empty() {
            Err(ProcessScanError::Unavailable(
                "Darwin libproc returned zero observations".into(),
            ))
        } else {
            Ok(observations)
        }
    }
}

impl ProcessScanner for DarwinProcessScanner {
    fn scan(&self) -> Result<Vec<ProcessObservation>, ProcessScanError> {
        #[cfg(target_os = "macos")]
        {
            match self.scan_native() {
                Ok(obs) => Ok(obs),
                Err(_) => {
                    // Fallback to ps if native libproc encounters sandbox/permission issues
                    let fallback = FallbackProcessScanner::new();
                    fallback.scan()
                }
            }
        }
        #[cfg(not(target_os = "macos"))]
        {
            let fallback = FallbackProcessScanner::new();
            fallback.scan()
        }
    }
}

/// Retrieve all live process IDs from Darwin `libproc`.
#[cfg(target_os = "macos")]
pub fn list_pids() -> Result<Vec<u32>, ProcessScanError> {
    unsafe {
        let count = proc_listpids(PROC_ALL_PIDS, 0, std::ptr::null_mut(), 0);
        if count <= 0 {
            return Err(ProcessScanError::Unavailable(
                "proc_listpids returned non-positive byte count".into(),
            ));
        }
        let num_pids = count as usize / std::mem::size_of::<pid_t>();
        let mut pids: Vec<pid_t> = vec![0; num_pids];
        let bytes = proc_listpids(
            PROC_ALL_PIDS,
            0,
            pids.as_mut_ptr() as *mut c_void,
            (pids.len() * std::mem::size_of::<pid_t>()) as i32,
        );
        if bytes <= 0 {
            return Err(ProcessScanError::Unavailable(
                "proc_listpids failed to retrieve pids".into(),
            ));
        }
        let actual_pids = bytes as usize / std::mem::size_of::<pid_t>();
        Ok(pids[..actual_pids]
            .iter()
            .filter_map(|&p| if p > 0 { Some(p as u32) } else { None })
            .collect())
    }
}

/// Query absolute executable path via `proc_pidpath`.
#[cfg(target_os = "macos")]
pub fn pid_path(pid: u32) -> Option<String> {
    let mut path_buf = [0u8; PROC_PIDPATHINFO_MAXSIZE as usize];
    let ret = unsafe {
        proc_pidpath(
            pid as pid_t,
            path_buf.as_mut_ptr() as *mut c_void,
            path_buf.len() as u32,
        )
    };
    if ret > 0 {
        let path_str = String::from_utf8_lossy(&path_buf[..ret as usize]).to_string();
        if !path_str.is_empty() {
            return Some(path_str);
        }
    }
    None
}

/// Query BSD process info (PPID and process name) via `proc_pidinfo`.
#[cfg(target_os = "macos")]
pub fn pid_info(pid: u32) -> Option<(u32, String)> {
    let mut bsd_info = std::mem::MaybeUninit::<proc_bsdinfo>::zeroed();
    let ret = unsafe {
        proc_pidinfo(
            pid as pid_t,
            PROC_PIDTBSDINFO,
            0,
            bsd_info.as_mut_ptr() as *mut libc::c_void,
            std::mem::size_of::<proc_bsdinfo>() as i32,
        )
    };
    if ret as usize == std::mem::size_of::<proc_bsdinfo>() {
        let info = unsafe { bsd_info.assume_init() };
        let ppid = info.pbi_ppid;
        let name_bytes: Vec<u8> = info
            .pbi_name
            .iter()
            .take_while(|&&c| c != 0)
            .map(|&c| c as u8)
            .collect();
        let name = String::from_utf8_lossy(&name_bytes).to_string();
        Some((ppid, name))
    } else {
        None
    }
}

/// Query command line arguments via `sysctl` (`KERN_PROCARGS2`).
#[cfg(target_os = "macos")]
pub fn pid_cmdline(pid: u32) -> Option<Vec<String>> {
    let mut mib = [CTL_KERN, KERN_PROCARGS2, pid as c_int];
    let mut size: libc::size_t = 0;
    let res = unsafe {
        sysctl(
            mib.as_mut_ptr(),
            3,
            std::ptr::null_mut(),
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    if res != 0 || size < 4 {
        return None;
    }
    let mut buffer: Vec<u8> = vec![0; size];
    let res = unsafe {
        sysctl(
            mib.as_mut_ptr(),
            3,
            buffer.as_mut_ptr() as *mut c_void,
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    if res != 0 || size < 4 {
        return None;
    }
    buffer.truncate(size);
    parse_procargs2(&buffer)
}

/// Parse Darwin `KERN_PROCARGS2` byte buffer into argument vector.
pub fn parse_procargs2(buffer: &[u8]) -> Option<Vec<String>> {
    if buffer.len() < 4 {
        return None;
    }
    let argc = i32::from_ne_bytes(buffer[0..4].try_into().ok()?);
    if argc <= 0 {
        return None;
    }
    let mut idx = 4;
    // Skip executable path (null terminated)
    while idx < buffer.len() && buffer[idx] != 0 {
        idx += 1;
    }
    // Skip trailing null padding bytes after executable path
    while idx < buffer.len() && buffer[idx] == 0 {
        idx += 1;
    }

    let mut args = Vec::with_capacity(argc as usize);
    while idx < buffer.len() && (args.len() as i32) < argc {
        let start = idx;
        while idx < buffer.len() && buffer[idx] != 0 {
            idx += 1;
        }
        let arg = String::from_utf8_lossy(&buffer[start..idx]).to_string();
        args.push(arg);
        idx += 1; // skip null byte
    }
    if args.is_empty() {
        None
    } else {
        Some(args)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_procargs2_buffer() {
        let mut buf = Vec::new();
        // argc = 3 (i32)
        buf.extend_from_slice(&3i32.to_ne_bytes());
        // executable path "/usr/local/bin/claude\0"
        buf.extend_from_slice(b"/usr/local/bin/claude\0");
        // padding nulls
        buf.extend_from_slice(b"\0\0\0");
        // argv[0] = "claude\0"
        buf.extend_from_slice(b"claude\0");
        // argv[1] = "--help\0"
        buf.extend_from_slice(b"--help\0");
        // argv[2] = "--verbose\0"
        buf.extend_from_slice(b"--verbose\0");
        // trailing env vars (should not be parsed)
        buf.extend_from_slice(b"SECRET_KEY=12345\0");

        let parsed = parse_procargs2(&buf).expect("buffer parsing should succeed");
        assert_eq!(parsed, vec!["claude", "--help", "--verbose"]);
    }
}
