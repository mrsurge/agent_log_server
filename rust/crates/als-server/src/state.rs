use crate::adapter_process::{AdapterEventSink, AdapterSupervisor};
use crate::config::ServerConfig;
use crate::conversation_store::ConversationStore;

#[derive(Clone)]
pub struct AppState {
    pub adapter: AdapterSupervisor,
    pub config: ServerConfig,
    pub conversations: ConversationStore,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        let events = AdapterEventSink::default();
        let adapter = AdapterSupervisor::new(config.clone(), events);
        let conversations = ConversationStore::new(config.roots.data_dir.clone());
        Self {
            adapter,
            config,
            conversations,
        }
    }
}
