# app/patches/apply_aliases.py
"""Build-time patch: inject kiro-* model aliases into vendored source."""
import sys
from pathlib import Path

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


def patch_config(config: Path) -> None:
    src = config.read_text()
    if CONFIG_SENTINEL in src:
        print("[skip] config.py already patched")
        return
    if "MODEL_ALIASES" not in src:
        sys.exit("config.py: MODEL_ALIASES not found")
    block = [CONFIG_SENTINEL, "MODEL_ALIASES.update({"]
    block += [f'    "{a}": "{t}",' for a, t in EXTRA_ALIASES.items()]
    block += ["})", "# <<< kiro-gateway custom aliases <<<"]
    config.write_text(src.rstrip() + "\n\n" + "\n".join(block) + "\n")
    print("[ok] patched MODEL_ALIASES")


def patch_resolver(resolver: Path) -> None:
    src = resolver.read_text()
    if RESOLVER_SENTINEL in src:
        print("[skip] model_resolver.py already patched")
        return
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
    resolver.write_text(src.replace(anchor, replacement, 1))
    print("[ok] patched get_model_id_for_kiro")


def main(vendor_root: Path) -> None:
    patch_config(vendor_root / "kiro" / "config.py")
    patch_resolver(vendor_root / "kiro" / "model_resolver.py")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
