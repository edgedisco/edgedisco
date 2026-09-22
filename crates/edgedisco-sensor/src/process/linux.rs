use crate::process::{ProcessObservation, ProcessScanError, ProcessScanner};
use std::path::{Path, PathBuf};

/// Linux `/proc` filesystem process scanner.
#[derive(Debug, Clone)]
pub struct LinuxProcessScanner {
    proc_root: PathBuf,
}

impl LinuxProcessScanner {
    pub fn new() -> Self {
        Self {
            proc_root: PathBuf::from("/proc"),
        }
    }

    pub fn with_root(root: impl Into<PathBuf>) -> Self {
        Self {
            proc_root: root.into(),
        }
    }

    /// Traverse directory tree mimicking Linux `/proc` and extract process observations.
    pub fn scan_dir(&self, root: &Path) -> Result<Vec<ProcessObservation>, ProcessScanError> {
        let read_dir = std::fs::read_dir(root).map_err(|e| {
            ProcessScanError::Unavailable(format!(
                "failed to read /proc directory {}: {e}",
                root.display()
            ))
        })?;

        let mut observations = Vec::new();

        for entry in read_dir {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            let file_name = entry.file_name();
            let name_str = file_name.to_string_lossy();
            let pid = match name_str.parse::<u32>() {
                Ok(p) => p,
                Err(_) => continue,
            };

            let proc_pid_dir = root.join(name_str.as_ref());

            // 1. Executable path from `exe` symlink
            let exe_link = proc_pid_dir.join("exe");
            let mut executable = std::fs::read_link(&exe_link)
                .ok()
                .and_then(|p| p.to_str().map(|s| s.to_string()))
                .unwrap_or_default();

            // 2. Stat / comm / ppid
            let mut ppid = None;
            let stat_path = proc_pid_dir.join("stat");
            if let Ok(stat_content) = std::fs::read_to_string(&stat_path) {
                if let Some(close_paren) = stat_content.rfind(')') {
                    if executable.is_empty() {
                        if let Some(open_paren) = stat_content.find('(') {
                            if open_paren < close_paren {
                                executable = stat_content[open_paren + 1..close_paren].to_string();
                            }
                        }
                    }
                    let after_paren = stat_content[close_paren + 1..].trim_start();
                    let fields: Vec<&str> = after_paren.split_whitespace().collect();
                    // fields[0] is state (e.g. 'R', 'S'), fields[1] is ppid
                    if fields.len() >= 2 {
                        ppid = fields[1].parse::<u32>().ok();
                    }
                }
            }

            if executable.is_empty() {
                let comm_path = proc_pid_dir.join("comm");
                if let Ok(comm) = std::fs::read_to_string(&comm_path) {
                    executable = comm.trim().to_string();
                }
            }

            if executable.is_empty() {
                continue;
            }

            // 3. Cmdline (null-byte separated arguments)
            let cmdline_path = proc_pid_dir.join("cmdline");
            let mut args = Vec::new();
            if let Ok(cmdline_bytes) = std::fs::read(&cmdline_path) {
                for token in cmdline_bytes.split(|&b| b == 0) {
                    if !token.is_empty() {
                        args.push(String::from_utf8_lossy(token).to_string());
                    }
                }
            }
            if args.is_empty() {
                args.push(executable.clone());
            }

            observations.push(ProcessObservation::new(Some(pid), ppid, executable, args));
        }

        if observations.is_empty() {
            Err(ProcessScanError::Unavailable(
                "no processes found in /proc".to_string(),
            ))
        } else {
            Ok(observations)
        }
    }
}

impl Default for LinuxProcessScanner {
    fn default() -> Self {
        Self::new()
    }
}

impl ProcessScanner for LinuxProcessScanner {
    fn scan(&self) -> Result<Vec<ProcessObservation>, ProcessScanError> {
        self.scan_dir(&self.proc_root)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{create_dir_all, write};
    use tempfile::tempdir;

    #[test]
    fn test_linux_proc_scanner_virtual_tree() {
        let temp = tempdir().expect("tempdir creation");
        let root = temp.path();

        // Simulate process 100
        let p100 = root.join("100");
        create_dir_all(&p100).expect("create p100");
        write(
            p100.join("stat"),
            "100 (systemd) S 0 100 100 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 0 0 0 0",
        )
        .expect("write stat");
        write(p100.join("comm"), "systemd\n").expect("write comm");
        write(p100.join("cmdline"), b"/lib/systemd/systemd\0--system\0").expect("write cmdline");

        // Simulate process 200 (ollama daemon)
        let p200 = root.join("200");
        create_dir_all(&p200).expect("create p200");
        write(
            p200.join("stat"),
            "200 (ollama) S 100 200 200 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 0 0 0 0",
        )
        .expect("write stat");
        write(p200.join("comm"), "ollama\n").expect("write comm");
        write(
            p200.join("cmdline"),
            b"/usr/local/bin/ollama\0serve\0--port\x0011434\0",
        )
        .expect("write cmdline");

        let scanner = LinuxProcessScanner::with_root(root);
        let observations = scanner.scan().expect("scanner should succeed");
        assert_eq!(observations.len(), 2);

        let ollama_obs = observations
            .iter()
            .find(|o| o.pid == Some(200))
            .expect("process 200 should be found");
        assert_eq!(ollama_obs.ppid, Some(100));
        assert_eq!(ollama_obs.executable, "ollama");
        assert_eq!(
            ollama_obs.args,
            vec!["/usr/local/bin/ollama", "serve", "--port", "11434"]
        );
    }
}
