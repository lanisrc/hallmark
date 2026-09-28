def is_remote_catalog(state):
    return "path" in state.data.columns and "sha1" not in state.data.columns
