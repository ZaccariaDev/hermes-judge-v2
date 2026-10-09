#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Model Router — Sélection intelligente des modèles cloud gratuits non-contributor.

Implements the directive from /home/zac/home/discord/hermes_provider_autonomy_goal.md:
- Allowlist of free non-contributor models
- Task-fit-based routing with Task Fit Matrix
- Dynamic health-aware scoring
- Fallback chain respecting capability equivalence
"""

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# ---------------------------------------------------------------------------
# Configuration — from the directive's allowlist
# ---------------------------------------------------------------------------

PRIMARY_MODEL = "meituan/longcat-2.5-preview:free"

# Exhaustive zero-price catalogue returned by Nous /v1/models on 2026-10-09.
# Verified live: only these 6 models are currently functional.
MODEL_SCORES: Dict[str, float] = {
    "meituan/longcat-2.5-preview:free": 98.0,
    "stepfun/step-5-preview:free": 94.0,
    "upstage/solar-mini4:free": 91.0,
    "inclusionai/ling-3.1-flash": 88.0,
    "poolside/laguna-s-2.1:free": 84.0,
    "stepfun/step-3.7-flash:free": 82.0,
}

ALLOWED_MODELS = list(MODEL_SCORES)
