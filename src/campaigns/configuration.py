"""YAML selection of the intent classifier, separate from audience policy."""

import math
import os
from pathlib import Path

from dotenv import dotenv_values
import yaml

from .clef import ClefModel
from .intents import IntentModel
from .service import IntentRouter
from .secrets import read_keyvault_secrets


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config/intent.yaml"


def _mapping(value, name, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"{name}: se requiere un mapa con campos válidos")
    return value


def _number(value, name, minimum, maximum, inclusive=True):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value > maximum
            or (value < minimum if inclusive else value <= minimum)):
        raise ValueError(f"{name}: valor numérico fuera de rango")
    return float(value)


def load_intent_config(path=DEFAULT_CONFIG):
    """Validate configuration without reading credentials or loading a model."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    except yaml.YAMLError:
        raise ValueError("Configuración de intenciones: YAML inválido") from None
    _mapping(document, "config", ("schema_version", "intent_classifier"))
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("schema_version debe ser 1")
    config = _mapping(document.get("intent_classifier"), "intent_classifier",
                      ("provider", "router", "tfidf", "clef"))
    if config.get("provider") not in ("tfidf", "clef"):
        raise ValueError("intent_classifier.provider debe ser tfidf o clef")
    if config.get("router", "hybrid") not in ("baseline", "learned", "hybrid"):
        raise ValueError("intent_classifier.router debe ser baseline, learned o hybrid")
    tfidf = _mapping(config.get("tfidf", {}), "tfidf", ("model_path",))
    model_path = tfidf.get("model_path", "outputs/models/intent.json")
    if not isinstance(model_path, str) or not model_path.strip():
        raise ValueError("tfidf.model_path debe ser una ruta no vacía")
    clef = _mapping(config.get("clef", {}), "clef",
                    ("timeout_seconds", "confidence_threshold", "margin_threshold"))
    return dict(provider=config["provider"], router=config.get("router", "hybrid"),
                tfidf=dict(model_path=model_path),
                clef=dict(timeout_seconds=_number(clef.get("timeout_seconds", 15),
                                                  "clef.timeout_seconds", 0, 60, False),
                          confidence_threshold=_number(clef.get("confidence_threshold", .5),
                                                       "clef.confidence_threshold", 0, 1),
                          margin_threshold=_number(clef.get("margin_threshold", .1),
                                                   "clef.margin_threshold", 0, 1)))


def create_intent_router(path=DEFAULT_CONFIG, *, root=ROOT, model_path=None, mode=None):
    """CLI model/router overrides take precedence; secrets stay out of YAML."""
    config = load_intent_config(path)
    root = Path(root)
    router_mode = mode if mode is not None else config["router"]
    if router_mode not in ("baseline", "learned", "hybrid"):
        raise ValueError("Unknown router mode")
    if router_mode == "baseline":
        return IntentRouter(None, router_mode)
    if config["provider"] == "tfidf":
        selected_path = Path(model_path if model_path is not None else config["tfidf"]["model_path"])
        if not selected_path.is_absolute():
            selected_path = root / selected_path
        model = IntentModel.load(selected_path)
    else:
        if model_path is not None:
            raise ValueError("--model solo se aplica al proveedor tfidf")
        if os.environ.get("AZURE_KEY_VAULT_URL") is not None:
            credentials = read_keyvault_secrets(("clef_api_token", "clef_Account_ID"))
            token, account_id = credentials["clef_api_token"], credentials["clef_Account_ID"]
        else:
            env_path = root / ".env"
            credentials = dotenv_values(env_path, encoding="utf-8-sig") if env_path.is_file() else {}
            token = os.environ.get("clef_api_token", credentials.get("clef_api_token"))
            account_id = os.environ.get("clef_Account_ID", credentials.get("clef_Account_ID"))
        model = ClefModel(token, account_id, **config["clef"])
    return IntentRouter(model, router_mode)
