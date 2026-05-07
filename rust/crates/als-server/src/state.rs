use crate::adapter_process::{AdapterEventSink, AdapterSupervisor};
use crate::config::ServerConfig;
use crate::conversation_store::ConversationStore;
use crate::extension_registry::ExtensionRegistry;
use crate::ipc::IpcClientStore;
use anyhow::{Result, anyhow};
use std::sync::{Arc, Mutex};
use tracing::warn;

#[derive(Clone)]
pub struct AppState {
    pub adapter: AdapterSupervisor,
    pub config: ServerConfig,
    pub conversations: ConversationStore,
    pub extensions: ExtensionRegistry,
    pub ipc_clients: IpcClientStore,
    pub ui_selection: UiSelectionStore,
}

impl AppState {
    pub fn new(config: ServerConfig) -> Self {
        let events = AdapterEventSink::default();
        let adapter = AdapterSupervisor::new(config.clone(), events);
        let conversations = ConversationStore::new(config.roots.data_dir.clone());
        let extensions = ExtensionRegistry::load_with_config(
            config.extensions_dir.clone(),
            Some(config.roots.config_dir.clone()),
        )
        .unwrap_or_else(|error| {
            warn!(
                error = %error,
                path = %config.extensions_dir.display(),
                "failed to load ALS-RS extension registry"
            );
            ExtensionRegistry::load_empty_with_config(
                config.extensions_dir.clone(),
                Some(config.roots.config_dir.clone()),
            )
        });
        let ipc_clients = IpcClientStore::default();
        let ui_selection = UiSelectionStore::default();
        Self {
            adapter,
            config,
            conversations,
            extensions,
            ipc_clients,
            ui_selection,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct UiSelectionSnapshot {
    pub active_conversation_id: Option<String>,
    pub active_view: String,
}

impl Default for UiSelectionSnapshot {
    fn default() -> Self {
        Self {
            active_conversation_id: None,
            active_view: "splash".to_owned(),
        }
    }
}

#[derive(Clone, Default)]
pub struct UiSelectionStore {
    inner: Arc<Mutex<UiSelectionSnapshot>>,
}

impl UiSelectionStore {
    pub fn snapshot(&self) -> Result<UiSelectionSnapshot> {
        let state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("ui selection lock poisoned"))?;
        Ok(state.clone())
    }

    pub fn select(
        &self,
        conversation_id: Option<String>,
        view: Option<String>,
    ) -> Result<UiSelectionSnapshot> {
        let mut state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("ui selection lock poisoned"))?;
        state.active_conversation_id = conversation_id;
        if let Some(view) = normalized_view(view.as_deref()) {
            state.active_view = view;
        }
        Ok(state.clone())
    }

    pub fn set_view(&self, view: Option<String>) -> Result<UiSelectionSnapshot> {
        let mut state = self
            .inner
            .lock()
            .map_err(|_| anyhow!("ui selection lock poisoned"))?;
        if let Some(view) = normalized_view(view.as_deref()) {
            state.active_view = view;
        }
        Ok(state.clone())
    }
}

fn normalized_view(view: Option<&str>) -> Option<String> {
    view.map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

#[cfg(test)]
mod tests {
    use super::{UiSelectionSnapshot, UiSelectionStore};

    #[test]
    fn ui_selection_defaults_to_splash_without_conversation() {
        let store = UiSelectionStore::default();
        assert_eq!(store.snapshot().unwrap(), UiSelectionSnapshot::default());
    }

    #[test]
    fn ui_selection_preserves_active_conversation_when_only_view_changes() {
        let store = UiSelectionStore::default();
        store
            .select(Some("conv-a".to_owned()), Some("conversation".to_owned()))
            .unwrap();
        let snapshot = store.set_view(Some("splash".to_owned())).unwrap();
        assert_eq!(snapshot.active_conversation_id.as_deref(), Some("conv-a"));
        assert_eq!(snapshot.active_view, "splash");
    }
}
