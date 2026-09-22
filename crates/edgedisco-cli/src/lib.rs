//! EdgeDisco CLI: Standalone native binary and service lifecycle CLI.

pub mod cli;
pub mod commands;
pub mod config;
pub mod ipc;
pub mod service;
pub mod util;

pub use cli::{Cli, Commands};
pub use service::{ServiceAction, ServiceError, ServiceExecutor, ServiceManager};
