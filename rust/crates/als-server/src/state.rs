use crate::adapter_process::{AdapterEventSink, AdapterSupervisor};
use crate::config::ServerConfig;

#[derive(Clone)]
pub struct AppState {
    pub adapter: AdapterSupervisor,
    pub config: ServerConfig,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        let events = AdapterEventSink::default();
        let adapter = AdapterSupervisor::new(config.clone(), events);
        Self { adapter, config }
    }
}
