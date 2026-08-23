from __future__ import annotations

from sqlite3 import Row


def task_score(task: Row | dict, energy_level: float) -> float:
    urgency = task["urgency"]
    importance = task["importance"]
    difficulty = task["difficulty"]

    if urgency >= 5 and importance >= 5:
        quadrant_weight = 1000
    elif urgency >= 5 and importance < 5:
        quadrant_weight = 500
    elif urgency < 5 and importance >= 5:
        quadrant_weight = 0
    else:
        quadrant_weight = -500

    normalized_energy = (energy_level - 5) / 5
    energy_difficulty_fit = 10 - abs(difficulty - energy_level)
    urgency_score = urgency * 18
    importance_score = importance * (10 + normalized_energy * 4)
    difficulty_energy_adjustment = (
        normalized_energy * difficulty * 14 + energy_difficulty_fit * 8
    )

    return (
        quadrant_weight
        + urgency_score
        + importance_score
        + difficulty_energy_adjustment
    )
