use crate::{
    conversation_rpc::register_conversations_rpc_namespace,
    ipc::register_ipc_namespace,
    settings_rpc::register_settings_rpc_namespace, ui_rpc::register_ui_rpc_namespace,
};
use socketioxide::SocketIo;

pub fn register_socket_namespaces(io: &SocketIo) {
    io.ns(
        "/",
        |_socket: socketioxide::extract::SocketRef| async move {},
    );
    register_ipc_namespace(io);
    register_conversations_rpc_namespace(io);
    register_settings_rpc_namespace(io);
    register_ui_rpc_namespace(io);
}
