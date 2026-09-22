//! EdgeDisco Core: Domain models, allowlisted AI detection catalog, embedded SQLite store, and redaction.

pub mod catalog;
pub mod models;
pub mod redaction;
pub mod store;

pub use catalog::*;
pub use models::*;
pub use redaction::*;
pub use store::*;
