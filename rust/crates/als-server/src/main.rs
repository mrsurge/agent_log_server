#[tokio::main]
async fn main() -> anyhow::Result<()> {
    als_server::run().await
}
