"""Local bilingual intent classifier; decisions and banking actions stay in service code.

Team-authored examples are supervised labels, not organizer/bank labels.  The
learned component is multinomial logistic regression on normalized TF-IDF word
and character n-grams.  JSON serialization intentionally avoids executable
pickle payloads.  No external model, network request or customer data is used.
"""

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import numpy as np


INTENTS = (
    "account_info", "activity_info", "advisor_request", "campaign_info",
    "commercial_terms", "greeting", "marketing_optout", "unknown",
    "unsupported_credit",
)
MODEL_VERSION = "tfidf-softmax-es-pt-v1"
BASELINE_VERSION = "keywords-es-pt-v1"
HYBRID_VERSION = "guarded-routing-es-pt-v1"
MAX_TEXT_CHARS = 2000
CONFIDENCE_THRESHOLD = 0.30
MARGIN_THRESHOLD = 0.10
MIN_FEATURE_COVERAGE = 0.08

_STOPWORDS = frozenset("a al as con da das de del do dos e el en es esta este la las lo los mi mis minha minhas meu meus na nas nos o os para por que qual quais un una um uma y yo eu me se si tem tengo do que quiero quero necesito preciso quisiera gostaria puedes pode podem dime diga mostrar mostre muestra ver saber consultar revisar entender".split())


def normalize(text):
    """Remove accents/case consistently; never alter the message in the service."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    decomposed = unicodedata.normalize("NFKD", text[:MAX_TEXT_CHARS].casefold())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(
        char for char in decomposed if not unicodedata.combining(char))))


def _features(text):
    words = [word for word in normalize(text).split() if word not in _STOPWORDS]
    result = Counter("w:" + word for word in words)
    result.update("b:" + left + " " + right for left, right in zip(words, words[1:]))
    # Character features share stems/typos across the two languages.  Boundaries
    # prevent accidental cross-word features, and character counts are downweighted.
    for word in words:
        padded = "^" + word + "$"
        for size in (3, 4):
            result.update("c:" + padded[pos:pos + size]
                          for pos in range(len(padded) - size + 1))
    return result


_PATTERNS = {
    "campaign_info": r"\b(campan\w*|oferta\w*|promoc\w*|promover\w*|promove\w*|publicidad|publicidade|publicitar\w*|anuncio\w*|anunci\w*|beneficio\w*|convite\w*|invitacion\w*|iniciativa|propuesta|proposta|comunicacion|comunicacao)\b",
    "account_info": r"\b(saldo\w*|cuenta\w*|conta\w*|poupanca|ahorro\w*|balance|balanco|producto\w*|produto\w*|fondos|fundos|economias)\b|\b(dinero|dinheiro)\b.*\b(disponible|disponivel)\b",
    "activity_info": r"\b(movimiento\w*|movimenta\w*|transac\w*|operacion\w*|operacao\w*|actividad|atividade|extracto\w*|extrato\w*|deposito\w*|retiro\w*|saque\w*|abono\w*|ingreso\w*|egreso\w*|historial|historico|entradas|salidas|saidas)\b|\b(ultima|ultimo|recent\w*)\b.*\b(uso|utilic\w*|utiliz\w*|dinero|dinheiro|credito)\b",
    "commercial_terms": r"\b(tasa\w*|taxa\w*|comision\w*|comisso\w*|tarifa\w*|costo\w*|coste\w*|custo\w*|interes(?:es|se|ses)?|juros|condicion\w*|condico\w*|requisito\w*|rentabilidad|rendimento\w*|ganancia\w*|ganho\w*|vigencia|garantiza\w*|garantid\w*|rentabiliza\w*|clausula\w*|cobro\w*|cobran\w*|cobra\w*|contrato\w*|restriccion\w*|restrico\w*|penalidad\w*|penalidade\w*|impuesto\w*|imposto\w*)\b",
    "advisor_request": r"\b(asesor\w*|assessor\w*|agente\w*|atendente\w*|humano\w*|persona|pessoa|especialista\w*|funcionario\w*|ejecutivo\w*|consultor\w*|empleado\w*|representante\w*|personalmente|pessoalmente|personal|pessoal|llamen|liguem)\b|\b(equipo|equipe|centro|central)\b.*\b(atencion|atendimento)\b|\b(llamar|ligar)\b.*\b(ayudar|ajudar|orientar)\b",
    "marketing_optout": r"\b(baja|cancelar|cancela\w*|cancelamento|unsubscribe|optout|desinscrib\w*|retir\w*|dejar|detener|parar|bloque\w*|elimin\w*|exclu\w*|quita\w*|revoc\w*|revog\w*)\b.*\b(publicidad|publicidade|campan\w*|mensaje\w*|mensage\w*|promoc\w*|oferta\w*|marketing|anuncio\w*|comercial\w*|comercia\w*|contacto|contato)\b|\b(no quiero|nao quero|nao desejo|no deseo|remov\w*|desativ\w*)\b.*\b(marketing|publicidad|publicidade|oferta\w*|campan\w*|mensage\w*|mensaje\w*|comercial\w*|comercia\w*|publicitar\w*)\b|\b(marketing|publicidad|publicidade|campan\w*)\b.*\b(desactivar|desativar|cancelar|cancelamento|baja)\b",
    "unsupported_credit": r"\b(credito\w*|prestamo\w*|prestad\w*|emprest\w*|tarjeta\w*|cartao\w*|financia\w*|hipoteca\w*|cupo|limite)\b",
}
_GREETING = re.compile(r"^(hola|buenos dias|buen dia|buenas tardes|buenas noches|buenas|saludos|saudac\w*|ola|oi|bom dia|boa tarde|boa noite|obrigad[oa]|gracias|muchas gracias|muito obrigad[oa]|te agradezco|agradec\w*|que tal|hello)( .*)?$")
_COMPILED = {intent: re.compile(pattern) for intent, pattern in _PATTERNS.items()}


def _keyword_hits(text):
    return {intent for intent, pattern in _COMPILED.items() if pattern.search(text)}


def baseline_predict(text):
    """Strong bilingual keyword baseline. Its confidence is a rule score, not probability.

    Explicit opt-out and unsupported credit outrank generic campaign/account words.
    An advisor request outranks its topic. Specific activity/terms outrank generic
    account/campaign mentions; unresolved multiple topics ask for clarification.
    """
    plain = normalize(text)
    hits = _keyword_hits(plain)
    if ("activity_info" in hits and re.search(r"\b(ultimo|ultima)\b.*\bcredito\b", plain)
            and not re.search(r"\b(prestamo|emprestimo|aprobar|aprovar|solicitar)\b", plain)):
        hits.discard("unsupported_credit")
    for priority in ("marketing_optout", "unsupported_credit", "advisor_request"):
        if priority in hits:
            return {"intent": priority, "confidence": 0.9, "ambiguous": False}
    if "commercial_terms" in hits:
        hits -= {"campaign_info", "account_info"}
    if "activity_info" in hits:
        hits.discard("account_info")
    if len(hits) == 1:
        return {"intent": next(iter(hits)), "confidence": 0.9, "ambiguous": False}
    if not hits and plain and _GREETING.fullmatch(plain):
        return {"intent": "greeting", "confidence": 0.9, "ambiguous": False}
    return {"intent": "unknown", "confidence": 0.0, "ambiguous": True}


def hybrid_predict(text, model):
    """Production-demo routing combines explicit rules with the learned suggestion.

    Model metrics still use model.predict alone.  Any baseline fallback/override
    is reported as such; its success must not be attributed to the learned model.
    Opt-out, human requests and unsupported credit are conservative rule routes.
    Other explicit rules fill abstentions, and disagreement asks for clarification.
    """
    learned, baseline = model.predict(text), baseline_predict(text)
    if baseline["intent"] in ("marketing_optout", "advisor_request", "unsupported_credit"):
        return {**baseline, "routing_source": "explicit_policy_route",
                "learned_intent": learned["intent"]}
    if learned["ambiguous"] and not baseline["ambiguous"]:
        return {**baseline, "routing_source": "baseline_fallback",
                "learned_intent": learned["intent"]}
    if (not baseline["ambiguous"] and not learned["ambiguous"]
            and baseline["intent"] != learned["intent"]):
        return {"intent": "unknown", "confidence": learned["confidence"],
                "ambiguous": True, "routing_source": "disagreement_clarification",
                "learned_intent": learned["intent"]}
    return {**learned, "routing_source": "learned", "learned_intent": learned["intent"]}


def _softmax(scores):
    exp = np.exp(scores - scores.max(axis=-1, keepdims=True))
    return exp / exp.sum(axis=-1, keepdims=True)


def read_examples(data_path):
    examples = []
    seen_ids, seen_text, family_splits = set(), set(), {}
    with Path(data_path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            required = ("id", "family_id", "language", "intent", "split", "source", "text")
            if any(not row.get(key) for key in required):
                raise ValueError(f"Missing example fields at line {line_number}")
            if row["intent"] not in INTENTS or row["language"] not in ("es", "pt"):
                raise ValueError(f"Unknown intent/language at line {line_number}")
            if row["split"] not in ("train", "development") or row["source"] != "team_authored":
                raise ValueError("Only authored training/development fixtures are permitted")
            plain = normalize(row["text"])
            if not plain or row["id"] in seen_ids or plain in seen_text:
                raise ValueError(f"Empty/duplicate training example at line {line_number}")
            prior = family_splits.setdefault(row["family_id"], row["split"])
            if prior != row["split"]:
                raise ValueError("A paraphrase family may not cross splits")
            seen_ids.add(row["id"])
            seen_text.add(plain)
            examples.append(row)
    for split in ("train", "development"):
        for intent in INTENTS:
            languages = {row["language"] for row in examples
                         if row["split"] == split and row["intent"] == intent}
            if languages != {"es", "pt"}:
                raise ValueError(f"Missing bilingual examples for {intent}/{split}")
    return examples


class IntentModel:
    """Learned routing suggestion. A confidence score never grants permission."""

    def __init__(self, artifact):
        if artifact.get("model_version") != MODEL_VERSION or artifact.get("intents") != list(INTENTS):
            raise ValueError("Unsupported or inconsistent intent model")
        self.artifact = artifact
        expected_thresholds = {"confidence": CONFIDENCE_THRESHOLD, "margin": MARGIN_THRESHOLD,
                               "minimum_feature_coverage": MIN_FEATURE_COVERAGE}
        if any(artifact.get("thresholds", {}).get(key) != value
               for key, value in expected_thresholds.items()):
            raise ValueError("Intent model threshold version differs from implementation")
        self.vocabulary = {feature: index for index, feature in enumerate(artifact["vocabulary"])}
        self.idf = np.asarray(artifact["idf"], dtype=np.float64)
        self.weights = np.asarray(artifact["weights"], dtype=np.float64)
        self.bias = np.asarray(artifact["bias"], dtype=np.float64)
        count = len(self.vocabulary)
        if (len(self.idf) != count or self.weights.shape != (count, len(INTENTS))
                or self.bias.shape != (len(INTENTS),) or count > 4500
                or not all(np.isfinite(array).all() for array in (self.idf, self.weights, self.bias))):
            raise ValueError("Invalid intent model dimensions or non-finite values")

    def _vector(self, text):
        features = _features(text)
        vector = np.zeros(len(self.vocabulary), dtype=np.float64)
        known = 0
        for feature, frequency in features.items():
            index = self.vocabulary.get(feature)
            if index is None:
                continue
            known += 1
            scale = 0.5 if feature.startswith("c:") else 1.0
            vector[index] = scale * (1 + math.log(frequency)) * self.idf[index]
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
        return vector, known / max(1, len(features))

    def predict(self, text):
        vector, coverage = self._vector(text)
        if not vector.any() or coverage < MIN_FEATURE_COVERAGE:
            return {"intent": "unknown", "confidence": 0.0, "ambiguous": True}
        probabilities = _softmax(vector @ self.weights + self.bias)
        order = np.argsort(probabilities, kind="stable")
        top, second = int(order[-1]), int(order[-2])
        score = float(probabilities[top])
        ambiguous = (score < CONFIDENCE_THRESHOLD or
                     score - float(probabilities[second]) < MARGIN_THRESHOLD)
        # Do not silently pick one of two independent requests.  This is the same
        # explicit multi-topic guard in the keyword baseline, not a learned policy.
        plain = normalize(text)
        hits = _keyword_hits(plain)
        if ("campaign_info" in hits and "account_info" in hits and
                re.search(r"\b(y|e|ademas|tambien|tambem)\b", plain) and
                not hits.intersection({"marketing_optout", "advisor_request", "commercial_terms", "activity_info", "unsupported_credit"})):
            ambiguous = True
        intent = INTENTS[top]
        return {"intent": "unknown" if ambiguous else intent,
                "confidence": round(score, 8), "ambiguous": ambiguous or intent == "unknown"}

    @classmethod
    def load(cls, model_path):
        path = Path(model_path)
        if path.stat().st_size > 20_000_000:
            raise ValueError("Intent model exceeds the local size limit")
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def train(cls, data_path, model_path):
        """Fit only train families; report development data without refitting on them."""
        examples = read_examples(data_path)
        train = [row for row in examples if row["split"] == "train"]
        document_frequency = Counter()
        for row in train:
            document_frequency.update(_features(row["text"]).keys())
        # Deterministic ties make reruns independent of hash/random iteration order.
        vocabulary = sorted(sorted(document_frequency,
                                   key=lambda key: (-document_frequency[key], key))[:4500])
        artifact = {
            "model_version": MODEL_VERSION, "baseline_version": BASELINE_VERSION,
            "hybrid_version": HYBRID_VERSION,
            "intents": list(INTENTS), "vocabulary": vocabulary,
            "idf": [1 + math.log((1 + len(train)) / (1 + document_frequency[key]))
                    for key in vocabulary],
            "weights": [[0.0] * len(INTENTS) for _ in vocabulary],
            "bias": [0.0] * len(INTENTS),
            "thresholds": {"confidence": CONFIDENCE_THRESHOLD, "margin": MARGIN_THRESHOLD,
                           "minimum_feature_coverage": MIN_FEATURE_COVERAGE,
                           "basis": "selected_with_authored_development_and_frozen_before_independent_held_out_evaluation"},
            "training": {"source": "team_authored_not_bank_labels", "split": "paraphrase_family",
                         "features": "TF-IDF word unigrams/bigrams and character trigrams/fourgrams",
                         "algorithm": "multinomial_logistic_regression_numpy",
                         "iterations": 900, "learning_rate": 2.0, "l2": 0.0001,
                         "randomness": "none_full_batch_zero_initialization", "rows": len(train),
                         "family_ids": sorted({row["family_id"] for row in train}),
                         "data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
                         "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                         "numpy_version": np.__version__},
            "limitations": ["Authored small bilingual fixtures are not observed bank demand.",
                            "Softmax confidence is not a calibrated correctness probability.",
                            "Development results do not estimate production quality.",
                            "Classifier does not generate facts, authenticate or execute banking actions.",
                            "Context/confirmation and all permissions are service-layer responsibilities."],
        }
        model = cls(artifact)
        matrix = np.vstack([model._vector(row["text"])[0] for row in train])
        targets = np.zeros((len(train), len(INTENTS)))
        for index, row in enumerate(train):
            targets[index, INTENTS.index(row["intent"])] = 1.0
        for _ in range(artifact["training"]["iterations"]):
            error = (_softmax(matrix @ model.weights + model.bias) - targets) / len(train)
            model.weights -= 2.0 * (matrix.T @ error + 0.0001 * model.weights)
            model.bias -= 2.0 * error.sum(axis=0)
        artifact["weights"] = model.weights.round(12).tolist()
        artifact["bias"] = model.bias.round(12).tolist()
        model = cls(artifact)
        development = [row for row in examples if row["split"] == "development"]
        artifact["development"] = {
            "split": "independent_paraphrase_families_no_refit", "rows": len(development),
            "family_ids": sorted({row["family_id"] for row in development}),
            "learned": evaluate(model.predict, development),
            "baseline": evaluate(baseline_predict, development),
            "hybrid": evaluate(lambda text: hybrid_predict(text, model), development),
        }
        artifact["representation_sha256"] = hashlib.sha256(json.dumps(
            {key: artifact[key] for key in ("model_version", "intents", "vocabulary", "idf",
                                           "weights", "bias", "thresholds")},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        destination = Path(model_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return cls(artifact)


def evaluate(predictor, examples):
    """Metrics count abstentions as unknown predictions; confidence is not accuracy."""
    confusion, languages, failures = defaultdict(Counter), defaultdict(list), []
    accepted, in_scope, accepted_in_scope, unknown_false_accepts = 0, 0, 0, 0
    for row in examples:
        prediction = predictor(row["text"])
        confusion[row["intent"]][prediction["intent"]] += 1
        is_accepted = prediction["intent"] != "unknown" and not prediction["ambiguous"]
        accepted += is_accepted
        in_scope += row["intent"] != "unknown"
        accepted_in_scope += is_accepted and row["intent"] != "unknown"
        unknown_false_accepts += is_accepted and row["intent"] == "unknown"
        languages[row["language"]].append((row["intent"], prediction["intent"]))
        if prediction["intent"] != row["intent"]:
            failures.append({"id": row["id"], "family_id": row["family_id"],
                             "language": row["language"], "expected": row["intent"],
                             "prediction": prediction})
    f1 = []
    for label in INTENTS:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in INTENTS if other != label)
        fn = sum(count for predicted, count in confusion[label].items() if predicted != label)
        f1.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0)
    correct = sum(confusion[label][label] for label in INTENTS)
    return {"n": len(examples), "correct": correct, "accuracy": correct / max(1, len(examples)),
            "macro_f1": sum(f1) / len(f1), "confusion": dict(confusion),
            "classification_coverage": accepted / max(1, len(examples)),
            "in_scope_classification_coverage": accepted_in_scope / max(1, in_scope),
            "unknown_false_accepts": unknown_false_accepts,
            "by_language": {language: {"n": len(rows), "accuracy": sum(
                expected == predicted for expected, predicted in rows) / len(rows)}
                for language, rows in sorted(languages.items())}, "failures": failures}
