"""Patch kiro-gateway in place so custom kiro-* aliases work end-to-end.

Two independent edits, BOTH required:

1. config.py — register our aliases into MODEL_ALIASES.

2. model_resolver.py — teach get_model_id_for_kiro() to consult MODEL_ALIASES.
   This is the helper the OpenAI/Anthropic converters actually call on the hot
   path (converters_*.py -> get_model_id_for_kiro -> Kiro API). The full
   ModelResolver.resolve() DOES consult aliases, but the converter path bypasses
   the resolver entirely, so the bare helper must be made alias-aware or our
   aliases never reach Kiro (request fails with HTTP 400 "Invalid model ID").

Both edits are idempotent (guarded by sentinels) and avoid brittle full-block
rewrites so they survive most upstream formatting changes.
"""
from pathlib import Path
import sys

CONFIG = Path("/app/kiro/config.py")
RESOLVER = Path("/app/kiro/model_resolver.py")

CONFIG_SENTINEL = "# >>> kiro-gateway custom aliases >>>"
RESOLVER_SENTINEL = "# kiro-gateway: alias-aware"

EXTRA_ALIASES = {
    "auto-kiro": "auto",
    "kiro-opus-4.8": "claude-opus-4.8",
    "kiro-opus-4.7": "claude-opus-4.7",
    "kiro-opus-4.6": "claude-opus-4.6",
    "kiro-sonnet-4.6": "claude-sonnet-4.6",
    "kiro-sonnet-4.5": "claude-sonnet-4.5",
    "kiro-haiku-4.5": "claude-haiku-4.5",
}


def patch_config() -> None:
    """Append `MODEL_ALIASES.update({...})` to the end of config.py.

    Append (not block-replace) so we don't depend on the dict's annotation
    style and don't clobber any aliases upstream ships by default.
    """
    src = CONFIG.read_text()
    if CONFIG_SENTINEL in src:
        print("[skip] config.py already patched")
        return
    if "MODEL_ALIASES" not in src:
        sys.exit("config.py: MODEL_ALIASES not found")

    block = [CONFIG_SENTINEL, "MODEL_ALIASES.update({"]
    block += [f'    "{alias}": "{target}",' for alias, target in EXTRA_ALIASES.items()]
    block += ["})", "# <<< kiro-gateway custom aliases <<<"]

    CONFIG.write_text(src.rstrip() + "\n\n" + "\n".join(block) + "\n")
    print("[ok] patched MODEL_ALIASES")


def patch_resolver() -> None:
    """Make get_model_id_for_kiro() resolve aliases before normalizing.

    Uses a function-local import so we don't depend on any specific top-level
    import line existing in model_resolver.py.
    """
    src = RESOLVER.read_text()
    if RESOLVER_SENTINEL in src:
        print("[skip] model_resolver.py already patched")
        return

    # Anchor on the first body line of get_model_id_for_kiro. Leading indent of
    # this line is preserved from the original source by the replace.
    anchor = (
        "normalized = normalize_model_name(model_name)\n"
        "    internal = hidden_models.get(normalized, normalized)"
    )
    if anchor not in src:
        sys.exit("model_resolver.py: get_model_id_for_kiro body not found")

    replacement = (
        "from kiro.config import MODEL_ALIASES  " + RESOLVER_SENTINEL + "\n"
        "    model_name = MODEL_ALIASES.get(model_name, model_name)\n"
        "    normalized = normalize_model_name(model_name)\n"
        "    internal = hidden_models.get(normalized, normalized)"
    )
    RESOLVER.write_text(src.replace(anchor, replacement, 1))
    print("[ok] patched get_model_id_for_kiro")


if __name__ == "__main__":
    patch_config()
    patch_resolver()
