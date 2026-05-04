use crate::adapter_process::{AdapterEventSink, AdapterSupervisor};
use crate::config::ServerConfig;
use crate::conversation_store::ConversationStore;
use crate::extension_registry::ExtensionRegistry;
use tracing::warn;

#[derive(Clone)]
pub struct AppState {
    pub adapter: AdapterSupervisor,
    pub config: ServerConfig,
    pub conversations: ConversationStore,
    pub extensions: ExtensionRegistry,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        let events = AdapterEventSink::default();
        let adapter = AdapterSupervisor::new(config.clone(), events);
        let conversations = ConversationStore::new(config.roots.data_dir.clone());
        let extensions =
            ExtensionRegistry::load(config.extensions_dir.clone()).unwrap_or_else(|error| {
                warn!(
                    error = %error,
                    path = %config.extensions_dir.display(),
                    "failed to load ALS-RS extension registry"
                );
                ExtensionRegistry::load_empty(config.extensions_dir.clone())
            });
        Self {
            adapter,
            config,
            conversations,
            extensions,
        }
    }
}
