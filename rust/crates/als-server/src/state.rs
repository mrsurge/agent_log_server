use crate::config::ServerConfig;

#[derive(Clone, Debug)]
pub struct AppState {
    pub config: ServerConfig,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        Self { config }
    }
}
