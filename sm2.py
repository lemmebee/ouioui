def sm2(quality: int, repetitions: int, easiness: float, interval: int) -> tuple[int, float, int]:
    """SM-2 spaced repetition algorithm.

    Args:
        quality: 0-5 rating (0=blackout, 5=perfect)
        repetitions: current successful repetition count
        easiness: current easiness factor (>=1.3)
        interval: current interval in days

    Returns:
        (new_repetitions, new_easiness, new_interval_days)
    """
    new_easiness = max(1.3, easiness + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))

    if quality < 3:
        new_repetitions = 0
        new_interval = 1
    else:
        new_repetitions = repetitions + 1
        if new_repetitions == 1:
            new_interval = 1
        elif new_repetitions == 2:
            new_interval = 6
        else:
            new_interval = round(interval * new_easiness)

    return new_repetitions, new_easiness, new_interval
