"""One canonical name and aggregation rule per health metric.

Three phone-side sources spell the same metric three different ways:

* Shortcuts sends display labels - ``Steps``, ``Resting Heart Rate``, ``Sleep``.
* Health Auto Export sends snake_case names - ``step_count``, ``resting_heart_rate``.
* The Health export XML sends HealthKit identifiers -
  ``HKQuantityTypeIdentifierStepCount``.

All three land on the same canonical key here, so a row's origin never changes
how it is stored or queried. Unknown metrics are kept rather than dropped: they
get a slug, a guessed unit, and an aggregation rule derived from the unit, so a
health metric Apple adds next year still lands in the database.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

# Aggregations. ``sum`` for counters (steps, energy, distance), ``avg`` for
# rates and levels (heart rate, SpO2), ``last`` for slow-moving body values
# (weight, VO2 max), ``max`` for one-number-per-activity peaks.
SUM, AVG, LAST, MAX, MIN = "sum", "avg", "last", "max", "min"


class Metric(NamedTuple):
    key: str
    label: str
    unit: str
    agg: str
    aliases: Tuple[str, ...] = ()
    #: Direction that is good news, for briefs and dashboards. None = no opinion.
    higher_is_better: Optional[bool] = None


def _slugs(*names: str) -> Tuple[str, ...]:
    out: List[str] = []
    for name in names:
        if name and name not in out:
            out.append(name)
    return tuple(out)


# The table. Order is irrelevant; lookup builds indexes from aliases.
_TABLE: Tuple[Metric, ...] = (
    # ── activity ────────────────────────────────────────────────────────────
    Metric("steps", "Steps", "count", SUM, _slugs("step", "stepcount", "step_count"), True),
    Metric("distance_walking_running", "Distance (walk + run)", "mi", SUM,
           _slugs("distancewalkingrunning", "distance_walking_and_running",
                  "walking_running_distance", "distance"), True),
    Metric("flights_climbed", "Flights climbed", "count", SUM, _slugs("flights", "flight"), True),
    Metric("active_energy", "Active energy", "Cal", SUM,
           _slugs("activeenergy", "activeenergyburned", "active_energy_burned", "calories", "energy"), True),
    Metric("basal_energy", "Resting energy", "Cal", SUM,
           _slugs("basalenergy", "basalenergyburned", "basal_energy_burned", "resting_energy"), True),
    Metric("exercise_minutes", "Exercise minutes", "min", SUM,
           _slugs("exercise", "exercise_time", "apple_exercise_time", "appleexercisetime",
                  "exerciseminutes", "apple_exercise_minutes"), True),
    Metric("stand_minutes", "Stand minutes", "min", SUM,
           _slugs("standtime", "applestandtime", "apple_stand_time", "standtime_minutes"), True),
    Metric("stand_hours", "Stand hours", "count", SUM,
           _slugs("standhours", "apple_stand_hour", "applestandhour", "stand"), True),
    Metric("time_in_daylight", "Time in daylight", "min", SUM,
           _slugs("timeindaylight", "daylight"), True),
    Metric("physical_effort", "Physical effort", "kcal/hr\u00b7kg", AVG,
           _slugs("physicaleffort", "effort"), None),
    Metric("move_minutes", "Move minutes", "min", SUM, _slugs("movetime", "moveminutes"), True),

    # ── heart ───────────────────────────────────────────────────────────────
    Metric("heart_rate", "Heart rate", "count/min", AVG, _slugs("heartrate", "hr"), None),
    Metric("resting_heart_rate", "Resting heart rate", "count/min", AVG,
           _slugs("restingheartrate", "restinghr", "rhr"), False),
    Metric("walking_heart_rate_average", "Walking heart rate", "count/min", AVG,
           _slugs("walkingheartrateaverage", "walkingheartrate"), False),
    Metric("hrv", "Heart rate variability (SDNN)", "ms", AVG,
           _slugs("heartratevariabilitysdn", "heartratevariability", "heartratevariabilitysdnn",
                  "hrv_sdnn", "sdnn"), True),
    Metric("heart_rate_recovery", "Heart rate recovery (1 min)", "count/min", AVG,
           _slugs("heartraterecoveryoneminute", "heartraterecovery", "cardio_recovery"), True),
    Metric("vo2_max", "VO\u2082 max", "mL/min\u00b7kg", LAST, _slugs("vo2max", "vo2", "cardiorespiratory_fitness"), True),
    Metric("blood_pressure_systolic", "Blood pressure (systolic)", "mmHg", AVG,
           _slugs("bloodpressuresystolic", "systolic"), None),
    Metric("blood_pressure_diastolic", "Blood pressure (diastolic)", "mmHg", AVG,
           _slugs("bloodpressurediastolic", "diastolic"), None),

    # ── respiratory / vitals ────────────────────────────────────────────────
    Metric("respiratory_rate", "Respiratory rate", "count/min", AVG, _slugs("respiratoryrate", "breathingrate"), None),
    Metric("oxygen_saturation", "Blood oxygen", "%", AVG,
           _slugs("oxygensaturation", "spo2", "bloodoxygen"), True),
    Metric("wrist_temperature", "Wrist temperature", "degF", AVG,
           _slugs("applesleepingwristtemperature", "sleepingwristtemperature", "wristtemperature"), None),
    Metric("sleeping_breathing_disturbances", "Breathing disturbances", "count", AVG,
           _slugs("applesleepingbreathingdisturbances", "sleepingbreathingdisturbances"), False),
    Metric("body_temperature", "Body temperature", "degF", LAST,
           _slugs("bodytemperature", "basalbodytemperature", "temperature"), None),

    # ── body ────────────────────────────────────────────────────────────────
    Metric("body_mass", "Weight", "lb", LAST,
           _slugs("bodymass", "weightbodymass", "weight", "mass"), None),
    Metric("body_fat_percentage", "Body fat", "%", LAST, _slugs("bodyfatpercentage", "bodyfat"), False),
    Metric("body_mass_index", "BMI", "count", LAST, _slugs("bodymassindex", "bmi"), None),
    Metric("lean_body_mass", "Lean body mass", "lb", LAST, _slugs("leanbodymass", "leanmass"), None),
    Metric("waist_circumference", "Waist circumference", "in", LAST, _slugs("waistcircumference", "waist"), False),
    Metric("height", "Height", "ft", LAST, _slugs("height"), None),
    Metric("walking_steadiness", "Walking steadiness", "%", LAST,
           _slugs("applewalkingsteadiness", "walkingsteadiness"), True),

    # ── gait & mobility ─────────────────────────────────────────────────────
    Metric("walking_speed", "Walking speed", "mi/hr", AVG, _slugs("walkingspeed", "gait_speed"), True),
    Metric("walking_step_length", "Step length", "in", AVG, _slugs("walkingsteplength", "steplength"), True),
    Metric("walking_double_support", "Double support", "%", AVG,
           _slugs("walkingdoublesupportpercentage", "walkingdoublesupport", "doublesupport"), False),
    Metric("walking_asymmetry", "Walking asymmetry", "%", AVG,
           _slugs("walkingasymmetrypercentage", "walkingasymmetry", "asymmetry"), False),
    Metric("stair_ascent_speed", "Stair ascent speed", "ft/s", AVG,
           _slugs("stairascentspeed", "stairsup"), True),
    Metric("stair_descent_speed", "Stair descent speed", "ft/s", AVG,
           _slugs("stairdescentspeed", "stairsdown"), True),
    Metric("six_minute_walk", "Six-minute walk distance", "m", LAST,
           _slugs("sixminutewalktestdistance", "sixminutewalkingtestdistance", "sixminutewalk",
                  "six_minute_walking_test_distance", "walktest"), True),

    # ── running ─────────────────────────────────────────────────────────────
    Metric("running_speed", "Running speed", "mi/hr", AVG, _slugs("runningspeed", "pace"), True),
    Metric("running_power", "Running power", "W", AVG, _slugs("runningpower"), True),
    Metric("running_stride_length", "Running stride length", "m", AVG,
           _slugs("runningstridelength", "stride_length"), True),
    Metric("running_ground_contact_time", "Ground contact time", "ms", AVG,
           _slugs("runninggroundcontacttime", "groundcontacttime"), False),
    Metric("running_vertical_oscillation", "Vertical oscillation", "cm", AVG,
           _slugs("runningverticaloscillation", "verticaloscillation"), False),

    # ── cycling / swimming / water ──────────────────────────────────────────
    Metric("cycling_speed", "Cycling speed", "mi/hr", AVG, _slugs("cyclingspeed"), True),
    Metric("cycling_cadence", "Cycling cadence", "count/min", AVG, _slugs("cyclingcadence"), True),
    Metric("cycling_power", "Cycling power", "W", AVG, _slugs("cyclingpower"), True),
    Metric("distance_cycling", "Distance (cycling)", "mi", SUM, _slugs("distancecycling"), True),
    Metric("distance_swimming", "Distance (swimming)", "yd", SUM, _slugs("distanceswimming"), True),
    Metric("swimming_strokes", "Swimming strokes", "count", SUM,
           _slugs("swimmingstrokecount", "swimmingstrokes", "strokecount"), True),
    Metric("underwater_depth", "Underwater depth", "ft", MAX, _slugs("underwaterdepth", "depth"), None),
    Metric("water_temperature", "Water temperature", "degF", AVG, _slugs("watertemperature"), None),

    # ── environment ─────────────────────────────────────────────────────────
    Metric("headphone_audio_exposure", "Headphone audio exposure", "dBASPL", AVG,
           _slugs("headphoneaudioexposure", "headphoneexposure"), False),
    Metric("environmental_audio_exposure", "Environmental audio exposure", "dBASPL", AVG,
           _slugs("environmentalaudioexposure", "environmentalaudio"), False),
    Metric("environmental_sound_reduction", "Sound reduction", "dBASPL", AVG,
           _slugs("environmentalsoundreduction", "soundreduction"), True),

    # ── nutrition (logged by hand, if at all) ───────────────────────────────
    Metric("dietary_energy", "Energy eaten", "Cal", SUM,
           _slugs("dietaryenergyconsumed", "dietaryenergy", "calories_consumed"), None),
    Metric("dietary_protein", "Protein", "g", SUM, _slugs("dietaryprotein", "protein"), None),
    Metric("dietary_carbohydrates", "Carbohydrates", "g", SUM,
           _slugs("dietarycarbohydrates", "carbs"), None),
    Metric("dietary_fat", "Fat", "g", SUM, _slugs("dietaryfat", "fat"), None),
    Metric("dietary_water", "Water", "fl_oz_us", SUM, _slugs("dietarywater", "water"), True),
    Metric("caffeine", "Caffeine", "mg", SUM, _slugs("dietarycaffeine", "caffeine"), None),
    Metric("sleep_goal", "Sleep goal", "hr", LAST, _slugs("sleepdurationgoal", "sleepgoal"), None),
)

METRICS: Dict[str, Metric] = {m.key: m for m in _TABLE}

#: Anything else is a special sink handled outside the sample table.
SLEEP_KEY = "sleep"
WORKOUT_KEY = "workouts"

# Phrases that mean "sleep" rather than a quantity. Shortcuts sends "Sleep" for
# the sleep analysis samples; HAE sends "sleep_analysis".
_SLEEP_ALIASES = ("sleep", "sleepanalysis", "sleep_analysis", "sleeptime", "timeasleep")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ALNUM = re.compile(r"[^a-z0-9]+")
# HealthKit identifiers arrive as HKQuantityTypeIdentifierStepCount /
# HKCategoryTypeIdentifierSleepAnalysis / HKDataTypeSleepDurationGoal. Strip the
# class prefix *before* splitting camel case, or the split turns it into
# "hk_quantity_type_identifier_..." and the prefix never matches.
_PREFIX_RE = re.compile(
    r"^hk(?:workout|quantity|category|characteristic|data)?(?:activity)?(?:type)?(?:identifier)?",
    re.IGNORECASE,
)


def slug(name: str) -> str:
    """A stable, lowercase key for any spelling of a metric name."""
    text = (name or "").strip()
    if not text:
        return ""
    text = _PREFIX_RE.sub("", text).strip(" _-")
    text = _CAMEL.sub("_", text)
    text = text.lower()
    text = _ALNUM.sub("_", text).strip("_")
    return text


def compact(name: str) -> str:
    """Alphanumerics only: ``Resting Heart Rate`` and ``restingHeartRate`` agree."""
    return _ALNUM.sub("", slug(name))


def _index() -> Dict[str, str]:
    table: Dict[str, str] = {}
    for metric in _TABLE:
        for candidate in (metric.key, metric.label, *metric.aliases):
            for key in (slug(candidate), compact(candidate)):
                if key:
                    table.setdefault(key, metric.key)
    # "sleep" variants are explicit so they never resolve to a quantity metric.
    for alias in _SLEEP_ALIASES:
        table[slug(alias)] = SLEEP_KEY
        table[compact(alias)] = SLEEP_KEY
    return table


_ALIAS_INDEX: Dict[str, str] = _index()


def guess_agg(unit: str) -> str:
    """Aggregation rule for an unknown metric, from its unit alone.

    A rate (``count/min``, ``mi/hr``, ``%``) is meaningless summed across a day,
    so anything per-time or a percentage averages; everything else is a counter.
    """
    u = (unit or "").lower()
    if "/" in u or u in {"%", "percent", "degf", "degc", "dbaspl", "db", "mmhg", "cm", "in", "kg", "lb"}:
        return AVG
    return SUM


def resolve(name: str, unit: str = "") -> Metric:
    """Canonical metric for any spelling. Unknown names become a derived metric."""
    key = slug(name)
    if not key:
        raise ValueError("metric name is required")
    canonical = _ALIAS_INDEX.get(key) or _ALIAS_INDEX.get(compact(name))
    if canonical == SLEEP_KEY:
        return Metric(SLEEP_KEY, "Sleep", "hr", SUM, higher_is_better=True)
    if canonical:
        return METRICS[canonical]
    return Metric(key, key.replace("_", " ").capitalize(), unit or "", guess_agg(unit))


def is_sleep(name: str) -> bool:
    return slug(name) in _SLEEP_ALIASES or compact(name) in _SLEEP_ALIASES or slug(name) == SLEEP_KEY


def all_keys() -> List[str]:
    return sorted(METRICS)


def describe() -> List[dict]:
    """Machine-readable metric table, for MCP tool listings and the README."""
    return [
        {
            "key": m.key,
            "label": m.label,
            "unit": m.unit,
            "aggregation": m.agg,
            "aliases": list(m.aliases),
        }
        for m in _TABLE
    ]


def match_keys(term: str) -> List[str]:
    """Metric keys whose name or alias contains ``term`` (case-insensitive)."""
    needle = (term or "").strip().lower()
    if not needle:
        return all_keys()
    hits = [m.key for m in _TABLE if needle in m.key or needle in m.label.lower()]
    for alias, key in _ALIAS_INDEX.items():
        if needle in alias and key not in hits:
            hits.append(key)
    return sorted(hits)


def sort_keys(keys: Iterable[str]) -> List[str]:
    order = {m.key: i for i, m in enumerate(_TABLE)}
    return sorted(keys, key=lambda k: (order.get(k, 10_000), k))


# ── sleep stages ────────────────────────────────────────────────────────────

IN_BED, ASLEEP, CORE, DEEP, REM, AWAKE = "in_bed", "asleep", "core", "deep", "rem", "awake"
SLEEP_STAGES = (IN_BED, ASLEEP, CORE, DEEP, REM, AWAKE)

_STAGE_TOKENS = (
    (IN_BED, ("inbed", "in_bed", "in bed", "bed", "im_bett")),
    (AWAKE, ("awake", "wake", "wach")),
    (DEEP, ("deep", "tief", "profond")),
    (REM, ("rem", "dream", "traum")),
    (CORE, ("core", "light", "leicht", "leger", "essentiel")),
    (ASLEEP, ("asleep", "asleepunspecified", "sleep", "unspecified", "asleep_unspecified")),
)


def sleep_stage(value: str) -> Optional[str]:
    """Normalize a sleep-stage label to a canonical stage.

    Apple localizes these strings, so match on substrings rather than a fixed
    table: ``HKCategoryValueSleepAnalysisAsleepDeep``, ``Deep``, ``ASLEEP DEEP``
    and a German phone's ``Tiefschlaf`` all resolve to ``deep``.
    """
    text = slug(value)
    if not text:
        return None
    for stage, tokens in _STAGE_TOKENS:
        for token in tokens:
            if token in text:
                return stage
    return ASLEEP


# ── workout activities ──────────────────────────────────────────────────────

_ACTIVITY_ALIASES = {
    "other": "other",
    "running": "running",
    "run": "running",
    "walking": "walking",
    "walk": "walking",
    "hiking": "hiking",
    "hike": "hiking",
    "cycling": "cycling",
    "biking": "cycling",
    "bike": "cycling",
    "swimming": "swimming",
    "swim": "swimming",
    "golf": "golf",
    "pickleball": "pickleball",
    "tennis": "tennis",
    "basketball": "basketball",
    "soccer": "soccer",
    "football": "football",
    "baseball": "baseball",
    "yoga": "yoga",
    "pilates": "pilates",
    "strengthtraining": "strength_training",
    "traditionalstrengthtraining": "strength_training",
    "functionalstrengthtraining": "strength_training",
    "highintensityintervaltraining": "hiit",
    "hiit": "hiit",
    "coretraining": "core_training",
    "rowing": "rowing",
    "elliptical": "elliptical",
    "stairstepper": "stair_climbing",
    "stairclimbing": "stair_climbing",
    "surfingsports": "surfing",
    "surfing": "surfing",
    "underwaterdiving": "scuba_diving",
    "scubadiving": "scuba_diving",
    "skiing": "skiing",
    "snowboarding": "snowboarding",
    "skating": "skating",
    "dance": "dance",
    "dancing": "dance",
    "mindandbody": "mind_and_body",
    "cooldown": "cooldown",
    "climbing": "climbing",
    "boxing": "boxing",
    "martialarts": "martial_arts",
    "tabletennis": "table_tennis",
    "badminton": "badminton",
    "volleyball": "volleyball",
    "trackandfield": "track_and_field",
    "play": "play",
    "wrestling": "wrestling",
    "waterfitness": "water_fitness",
    "waterpolo": "water_polo",
    "watersports": "water_sports",
    "paddleboarding": "paddleboarding",
    "fishing": "fishing",
    "hunting": "hunting",
    "equestriansports": "equestrian",
}


_ACTIVITY_INDEX: Dict[str, str] = {}
for _alias, _canonical in _ACTIVITY_ALIASES.items():
    for _key in (slug(_alias), _ALNUM.sub("", slug(_alias))):
        if _key:
            _ACTIVITY_INDEX.setdefault(_key, _canonical)


def activity_slug(value: str) -> str:
    """Normalize a workout activity to a slug.

    ``HKWorkoutActivityTypeRunning``, ``TraditionalStrengthTraining``,
    ``traditional_strength_training`` and ``Running`` all land on the same slug,
    because the three phone-side sources each spell it differently.
    """
    text = slug(value)
    flat = _ALNUM.sub("", text)
    if text in _ACTIVITY_INDEX:
        return _ACTIVITY_INDEX[text]
    if flat in _ACTIVITY_INDEX:
        return _ACTIVITY_INDEX[flat]
    for alias, canonical in _ACTIVITY_INDEX.items():
        if alias and alias in flat:
            return canonical
    return text or "other"
