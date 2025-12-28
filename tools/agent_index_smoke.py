#!/usr/bin/env python3
from pathlib import Path

from agent_index import (
    get_agent_cache_root,
    get_agent_index_dir,
    get_repo_agent_index_dir,
    install_templates_to_cache,
    load_agent_registry,
)


def main() -> None:
    repo_templates = get_repo_agent_index_dir(Path.cwd())
    installed = install_templates_to_cache(repo_root=Path.cwd(), overwrite=False)
    registry = load_agent_registry()

    print("Agent index smoke test")
    print(f"- cache_root: {get_agent_cache_root()}")
    print(f"- cache_index: {get_agent_index_dir()}")
    print(f"- repo_templates: {repo_templates}")
    print(f"- templates_installed: {len(installed)}")
    print(f"- agents_loaded: {len(registry)}")

    if registry:
        print("- agents:")
        for record in registry:
            print(f"  - {record.agent_id} (enabled={record.enabled}, profiles={record.profiles})")


if __name__ == "__main__":
    main()
