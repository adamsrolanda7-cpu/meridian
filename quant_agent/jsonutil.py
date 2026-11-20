"""Sérialisation JSON stable pour les runs, l'audit et les quotas.

Les imports numpy/pandas sont paresseux : les commandes légères du CLI
(`strategies`, `usage`, `tools`) ne paient pas le coût d'import des libs
scientifiques.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """Convertit récursivement les types numpy/pandas en types JSON."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(key): to_jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(value) for value in obj]

    module = type(obj).__module__
    if module.startswith("numpy"):
        import numpy as np  # import paresseux

        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            value = float(obj)
            return value if math.isfinite(value) else None
        if isinstance(obj, np.ndarray):
            return [to_jsonable(value) for value in obj.tolist()]
    if module.startswith("pandas"):
        import pandas as pd  # import paresseux

        if isinstance(obj, pd.Series):
            return [to_jsonable(value) for value in obj.tolist()]
        if isinstance(obj, pd.DataFrame):
            return [
                {str(key): to_jsonable(value) for key, value in row.items()}
                for row in obj.to_dict(orient="records")
            ]
        if isinstance(obj, pd.Timestamp):
            return None if pd.isna(obj) else obj.isoformat()
    return str(obj)


def canonical_json(payload: Any) -> str:
    """JSON canonique : clés triées, séparateurs compacts, Unicode conservé."""
    return json.dumps(
        to_jsonable(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def stable_hash(payload: Any, length: int = 12) -> str:
    """Hash court et stable d'un payload JSON."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:length]
